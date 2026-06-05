"""Read-only scraper for 1xbet match pages — Phase 0, REQ-013.

1xbet is the **only** Phase 0 source for BTTS and corner totals (decision #20):
the-odds-api exposes neither for ``soccer_fifa_world_cup``. The scraper is
read-only — no bets are placed programmatically.

Architecture (Plan A from Step 5.3 discussion):

* :class:`OneXBetClient` performs the HTTP fetch with realistic browser
  headers and detects Cloudflare challenge pages, raising
  :class:`CloudflareBlocked`. It delegates parsing to :func:`parse_markets_html`
  so tests can exercise the parser without HTTP.
* :func:`parse_markets_html` extracts a JSON island from a
  ``<script id="__INITIAL_STATE__" type="application/json">…</script>`` tag
  (the standard SPA hydration pattern) and normalises it into a list of
  :class:`OutcomeQuote` rows. Unsupported markets are silently dropped so
  partial coverage propagates as an absence in ``ParsedMarkets.quotes``.
* :func:`record_market_availability` writes one row to ``market_availability``
  per ``(match_id, market)`` per probe. It is idempotent on the natural key
  ``(match_id, market, observed_at)``. The API layer (Step 7) calls this
  whenever a scraper attempt either succeeds or raises
  :class:`CloudflareBlocked`, so the UI can flag indicative-only markets.
* :func:`probe` is the ``python -m predictor.odds.one_x_bet probe`` CLI:
  fetches a URL, writes ``reports/scraper-status.md`` summarising the
  attempt. It does **not** touch the DB — that is the API layer's job.

Market label conventions match the rest of the schema (``OddsSnapshot.market``):

* ``h2h`` → outcomes ``home`` / ``draw`` / ``away``
* ``totals_{point:g}`` → outcomes ``over`` / ``under``
* ``btts`` → outcomes ``yes`` / ``no``
* ``corners_{point:g}`` → outcomes ``over`` / ``under``
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
from sqlmodel import Session, select

from predictor.db.models import MarketAvailability

__all__ = [
    "BROWSER_HEADERS",
    "CloudflareBlocked",
    "OneXBetClient",
    "OneXBetError",
    "OutcomeQuote",
    "ParsedMarkets",
    "parse_markets_html",
    "probe",
    "record_market_availability",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OneXBetError(RuntimeError):
    """Non-Cloudflare scrape failure (network error, unparseable page, …)."""


class CloudflareBlocked(OneXBetError):
    """1xbet response was a Cloudflare challenge page.

    The scraper cannot solve the challenge in CI; callers should record a
    :class:`MarketAvailability` row with ``reason="cloudflare_blocked"`` for
    each affected market and surface an "indicative — no book" badge in the
    UI.
    """

    def __init__(self, detail: str = "1xbet response was a Cloudflare challenge") -> None:
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Parsed shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeQuote:
    """One ``(market, outcome, decimal_odds)`` row from a 1xbet match page."""

    market: str
    outcome: str
    decimal_odds: float


@dataclass(frozen=True)
class ParsedMarkets:
    """All quotes successfully parsed from one match page."""

    home_team: str
    away_team: str
    quotes: tuple[OutcomeQuote, ...] = field(default_factory=tuple)

    def markets_present(self) -> set[str]:
        return {q.market for q in self.quotes}


# ---------------------------------------------------------------------------
# HTML / JSON parsing
# ---------------------------------------------------------------------------


_INITIAL_STATE_RE = re.compile(
    r'<script\s+id="__INITIAL_STATE__"\s+type="application/json">'
    r"(?P<json>.*?)</script>",
    flags=re.DOTALL,
)

# Cloudflare challenge fingerprints — any one is enough to flag the page.
_CF_FINGERPRINTS = (
    "challenge-platform",
    "cf-chl",
    "_cf_chl_opt",
    "Just a moment...",
    "cf-mitigated",
)


def _looks_like_cloudflare(html: str) -> bool:
    return any(token in html for token in _CF_FINGERPRINTS)


def _extract_initial_state(html: str) -> dict[str, object]:
    match = _INITIAL_STATE_RE.search(html)
    if match is None:
        raise OneXBetError("no __INITIAL_STATE__ JSON island found")
    try:
        payload = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise OneXBetError(f"__INITIAL_STATE__ JSON was unparseable: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise OneXBetError("__INITIAL_STATE__ JSON root must be an object")
    return payload


_H2H_LABELS = {"W1": "home", "X": "draw", "W2": "away"}
_OVER_UNDER = {"over": "over", "under": "under"}
_YES_NO = {"yes": "yes", "no": "no"}


def _normalise_market(raw: dict[str, object]) -> Iterable[OutcomeQuote]:
    name = str(raw.get("name", "")).strip()
    outcomes_raw = raw.get("outcomes")
    if not isinstance(outcomes_raw, list):
        return ()
    line = raw.get("line")

    if name == "1X2":
        return _emit_h2h(outcomes_raw)
    if name == "Total" and isinstance(line, int | float):
        return _emit_over_under(f"totals_{float(line):g}", outcomes_raw)
    if name == "Total Corners" and isinstance(line, int | float):
        return _emit_over_under(f"corners_{float(line):g}", outcomes_raw)
    if name == "Both Teams To Score":
        return _emit_yes_no("btts", outcomes_raw)
    return ()


def _emit_h2h(outcomes_raw: list[object]) -> Iterable[OutcomeQuote]:
    for item in outcomes_raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        outcome = _H2H_LABELS.get(label)
        odds = item.get("odds")
        if outcome is None or not isinstance(odds, int | float):
            continue
        yield OutcomeQuote(market="h2h", outcome=outcome, decimal_odds=float(odds))


def _emit_over_under(market: str, outcomes_raw: list[object]) -> Iterable[OutcomeQuote]:
    for item in outcomes_raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip().lower()
        outcome = _OVER_UNDER.get(label)
        odds = item.get("odds")
        if outcome is None or not isinstance(odds, int | float):
            continue
        yield OutcomeQuote(market=market, outcome=outcome, decimal_odds=float(odds))


def _emit_yes_no(market: str, outcomes_raw: list[object]) -> Iterable[OutcomeQuote]:
    for item in outcomes_raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip().lower()
        outcome = _YES_NO.get(label)
        odds = item.get("odds")
        if outcome is None or not isinstance(odds, int | float):
            continue
        yield OutcomeQuote(market=market, outcome=outcome, decimal_odds=float(odds))


def parse_markets_html(html: str) -> ParsedMarkets:
    """Parse a 1xbet match-page HTML body into typed market quotes.

    Raises :class:`CloudflareBlocked` if the body is a CF challenge, and
    :class:`OneXBetError` if the JSON island is missing or unparseable.
    Unsupported markets are silently dropped so the caller can detect
    partial coverage via :meth:`ParsedMarkets.markets_present`.
    """
    if _looks_like_cloudflare(html):
        raise CloudflareBlocked()
    payload = _extract_initial_state(html)
    event = payload.get("event")
    if not isinstance(event, dict):
        raise OneXBetError("event block missing from __INITIAL_STATE__")
    home = str(event.get("home", "")).strip()
    away = str(event.get("away", "")).strip()
    if not home or not away:
        raise OneXBetError("event.home and event.away are required")

    raw_markets = payload.get("markets")
    if not isinstance(raw_markets, list):
        raise OneXBetError("markets block missing from __INITIAL_STATE__")

    quotes: list[OutcomeQuote] = []
    for raw in raw_markets:
        if not isinstance(raw, dict):
            continue
        quotes.extend(_normalise_market(raw))

    return ParsedMarkets(home_team=home, away_team=away, quotes=tuple(quotes))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class OneXBetClient:
    """Thin HTTP wrapper that fetches one match page and parses it."""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_match_markets(self, url: str) -> ParsedMarkets:
        """Fetch ``url`` and return parsed market quotes.

        Cloudflare detection runs on both the response headers (status 403
        / 503 + cf signals) and the body (``parse_markets_html`` rechecks).
        """
        response = self._client.get(url)
        if _response_is_cloudflare(response):
            raise CloudflareBlocked(
                f"1xbet returned a Cloudflare challenge (status={response.status_code})"
            )
        if response.status_code >= 400:
            raise OneXBetError(
                f"1xbet returned status {response.status_code}: {response.text[:200]}"
            )
        return parse_markets_html(response.text)


def _response_is_cloudflare(response: httpx.Response) -> bool:
    if response.status_code in (403, 503):
        server = response.headers.get("server", "").lower()
        if "cloudflare" in server:
            return True
        if response.headers.get("cf-mitigated"):
            return True
    return _looks_like_cloudflare(response.text)


# ---------------------------------------------------------------------------
# Persistence: market_availability
# ---------------------------------------------------------------------------


def record_market_availability(
    session: Session,
    *,
    match_id: int,
    market: str,
    available: bool,
    reason: str | None,
    observed_at: datetime,
) -> MarketAvailability | None:
    """Insert a ``market_availability`` row, idempotent on the natural key.

    Returns the persisted row, or ``None`` if a row with the same
    ``(match_id, market, observed_at)`` already exists. The caller is
    responsible for ``session.commit()``.
    """
    existing = session.exec(
        select(MarketAvailability).where(
            MarketAvailability.match_id == match_id,
            MarketAvailability.market == market,
            MarketAvailability.observed_at == observed_at,
        )
    ).first()
    if existing is not None:
        return None
    row = MarketAvailability(
        match_id=match_id,
        market=market,
        available=available,
        reason=reason,
        observed_at=observed_at,
    )
    session.add(row)
    return row


# ---------------------------------------------------------------------------
# Probe CLI — writes a markdown status report, no DB writes
# ---------------------------------------------------------------------------


_REPORT_TEMPLATE = """# 1xbet scraper status

- **Probed URL**: {url}
- **Observed at (UTC)**: {observed_at}
- **Outcome**: {outcome}
{detail}
"""


def probe(
    *,
    url: str,
    out_path: Path,
    client: OneXBetClient | None = None,
    now: datetime | None = None,
) -> Path:
    """Probe one 1xbet URL and write a markdown status report.

    Returns the report path. Does not raise on Cloudflare or HTTP errors —
    those are recorded in the report so CI can run the probe without
    failing the build.
    """
    # Naive UTC to match the rest of the schema (Match.kickoff_utc, OddsSnapshot.fetched_at).
    observed_at = now or datetime.now()
    owns_client = client is None
    scraper = client or OneXBetClient()
    try:
        parsed = scraper.fetch_match_markets(url)
        markets = sorted(parsed.markets_present())
        outcome = "ok"
        detail = (
            f"- **Teams**: {parsed.home_team} vs {parsed.away_team}\n"
            f"- **Markets parsed**: {', '.join(markets) if markets else '(none)'}\n"
            f"- **Quotes**: {len(parsed.quotes)}"
        )
    except CloudflareBlocked as exc:
        outcome = "cloudflare_blocked"
        detail = f"- **Reason**: {exc}"
    except OneXBetError as exc:
        outcome = "error"
        detail = f"- **Reason**: {exc}"
    finally:
        if owns_client:
            scraper.close()

    report = _REPORT_TEMPLATE.format(
        url=url,
        observed_at=observed_at.isoformat(timespec="seconds"),
        outcome=outcome,
        detail=detail,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return out_path


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="predictor.odds.one_x_bet")
    sub = parser.add_subparsers(dest="command", required=True)
    probe_cmd = sub.add_parser("probe", help="Fetch a 1xbet URL and write a status report.")
    probe_cmd.add_argument("--url", required=True, help="1xbet match-page URL")
    probe_cmd.add_argument(
        "--out",
        default="reports/scraper-status.md",
        help="Output report path (default: reports/scraper-status.md)",
    )
    args = parser.parse_args(argv)
    if args.command == "probe":
        probe(url=args.url, out_path=Path(args.out))
        return 0
    return 1  # pragma: no cover — argparse already rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

"""Typed client for the-odds-api v4 and persistence into ``odds_snapshots``.

The-odds-api exposes ``GET /v4/sports/{sport_key}/odds`` returning a list of
events; each event carries one or more bookmakers and each bookmaker one or
more market blocks (``h2h``, ``totals``, …). Outcomes inside a market are
labelled by **team name** (for ``h2h``) or by ``Over`` / ``Under`` (for
``totals``, with a ``point`` value attached).

The persistence layer maps this into the project's canonical natural key
``(match_id, book, market, outcome, fetched_at)`` on the ``odds_snapshots``
table:

* ``market`` for ``totals`` folds the ``point`` into the label, e.g.
  ``totals_2.5`` — this matches the rest of the schema (Decision in
  ``db.models.OddsSnapshot``).
* ``outcome`` for ``h2h`` is ``home`` / ``draw`` / ``away`` (resolved by
  matching ``outcome.name`` against the event's home / away team).
* Events are resolved against the ``matches`` table by exact
  ``(home_team, away_team, kickoff_utc == commence_time)`` join — if no
  match exists yet (schedule not ingested), the event is skipped and
  surfaced in :class:`SnapshotWriteResult.events_unmatched`.

Re-running ``persist_snapshots`` with the same ``fetched_at`` is a no-op:
the natural key is checked before insert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from predictor.db.models import Match, OddsSnapshot, Team

__all__ = [
    "BASE_URL",
    "SPORT_KEY_WC",
    "OddsApiBookmaker",
    "OddsApiError",
    "OddsApiEvent",
    "OddsApiMarket",
    "OddsApiOutcome",
    "SnapshotWriteResult",
    "TheOddsApiClient",
    "persist_snapshots",
]

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY_WC = "soccer_fifa_world_cup"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OddsApiError(RuntimeError):
    """Non-2xx response from the-odds-api."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"the-odds-api returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Wire-format models
# ---------------------------------------------------------------------------


class OddsApiOutcome(BaseModel):
    name: str
    price: float
    point: float | None = None


class OddsApiMarket(BaseModel):
    key: str
    last_update: datetime
    outcomes: list[OddsApiOutcome]


class OddsApiBookmaker(BaseModel):
    key: str
    title: str
    last_update: datetime
    markets: list[OddsApiMarket] = Field(default_factory=list)


class OddsApiEvent(BaseModel):
    id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: list[OddsApiBookmaker] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class TheOddsApiClient:
    """Thin typed wrapper over the-odds-api v4 ``/odds`` endpoint.

    The client is reusable and supports the context-manager protocol so the
    underlying ``httpx.Client`` is closed deterministically. Quota headers
    (``x-requests-remaining`` / ``x-requests-used``) from the last response
    are cached on the instance for caller-side logging / back-off.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        http_client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)
        self.requests_remaining: int | None = None
        self.requests_used: int | None = None

    # ------------------------------------------------------------------ ctx
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

    # ----------------------------------------------------------------- fetch
    def fetch_odds(
        self,
        *,
        sport_key: str = SPORT_KEY_WC,
        markets: list[str],
        regions: list[str],
        odds_format: str = "decimal",
    ) -> list[OddsApiEvent]:
        """Fetch live odds for ``sport_key`` across the requested markets.

        Raises :class:`OddsApiError` on any non-2xx response.
        """
        if not markets:
            raise ValueError("markets must be non-empty")
        if not regions:
            raise ValueError("regions must be non-empty")
        url = f"{self._base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": self._api_key,
            "regions": ",".join(regions),
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
        }
        response = self._client.get(url, params=params)
        self._update_quota(response)
        if response.status_code >= 400:
            raise OddsApiError(response.status_code, response.text)
        payload = response.json()
        if not isinstance(payload, list):
            raise OddsApiError(response.status_code, f"unexpected payload shape: {type(payload)}")
        return [OddsApiEvent.model_validate(item) for item in payload]

    def _update_quota(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-requests-remaining")
        used = response.headers.get("x-requests-used")
        self.requests_remaining = int(remaining) if remaining is not None else None
        self.requests_used = int(used) if used is not None else None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@dataclass
class SnapshotWriteResult:
    snapshots_added: int = 0
    snapshots_skipped_existing: int = 0
    events_unmatched: list[str] = field(default_factory=list)


def _resolve_outcome(
    market_key: str,
    outcome: OddsApiOutcome,
    *,
    home_team: str,
    away_team: str,
) -> tuple[str, str] | None:
    """Map an API outcome to ``(market_label, outcome_label)``.

    Returns ``None`` if the market / outcome combination isn't recognised so
    the caller can skip and surface a warning.
    """
    if market_key == "h2h":
        if outcome.name == home_team:
            return "h2h", "home"
        if outcome.name == away_team:
            return "h2h", "away"
        if outcome.name.lower() == "draw":
            return "h2h", "draw"
        return None
    if market_key == "totals":
        if outcome.point is None:
            return None
        label = f"totals_{outcome.point:g}"
        if outcome.name.lower() == "over":
            return label, "over"
        if outcome.name.lower() == "under":
            return label, "under"
        return None
    if market_key == "btts":
        if outcome.name.lower() == "yes":
            return "btts", "yes"
        if outcome.name.lower() == "no":
            return "btts", "no"
        return None
    return None


def _resolve_match(
    session: Session,
    *,
    home_team: str,
    away_team: str,
    commence_time: datetime,
) -> int | None:
    """Look up a ``matches.id`` by exact (home, away, kickoff) tuple."""
    home_id = session.exec(select(Team.id).where(Team.name == home_team)).first()
    away_id = session.exec(select(Team.id).where(Team.name == away_team)).first()
    if home_id is None or away_id is None:
        return None
    match_id = session.exec(
        select(Match.id).where(
            Match.home_team_id == home_id,
            Match.away_team_id == away_id,
            Match.kickoff_utc == commence_time,
        )
    ).first()
    return match_id


def persist_snapshots(
    session: Session,
    events: list[OddsApiEvent],
    *,
    fetched_at: datetime,
) -> SnapshotWriteResult:
    """Persist API events into ``odds_snapshots``.

    Idempotent: a row with the same
    ``(match_id, book, market, outcome, fetched_at)`` natural key is skipped
    rather than duplicated. The caller is responsible for ``session.commit()``.
    """
    result = SnapshotWriteResult()
    for event in events:
        # Strip tz so naive comparison against SQLite-stored datetimes works.
        commence_time = event.commence_time.replace(tzinfo=None)
        match_id = _resolve_match(
            session,
            home_team=event.home_team,
            away_team=event.away_team,
            commence_time=commence_time,
        )
        if match_id is None:
            result.events_unmatched.append(event.id)
            continue
        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                for outcome in market.outcomes:
                    mapped = _resolve_outcome(
                        market.key,
                        outcome,
                        home_team=event.home_team,
                        away_team=event.away_team,
                    )
                    if mapped is None:
                        continue
                    market_label, outcome_label = mapped
                    existing = session.exec(
                        select(OddsSnapshot).where(
                            OddsSnapshot.match_id == match_id,
                            OddsSnapshot.book == bookmaker.key,
                            OddsSnapshot.market == market_label,
                            OddsSnapshot.outcome == outcome_label,
                            OddsSnapshot.fetched_at == fetched_at,
                        )
                    ).one_or_none()
                    if existing is not None:
                        result.snapshots_skipped_existing += 1
                        continue
                    session.add(
                        OddsSnapshot(
                            match_id=match_id,
                            book=bookmaker.key,
                            market=market_label,
                            outcome=outcome_label,
                            decimal_odds=outcome.price,
                            fetched_at=fetched_at,
                        )
                    )
                    result.snapshots_added += 1
    return result

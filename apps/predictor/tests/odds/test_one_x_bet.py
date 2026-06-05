"""Tests for the 1xbet read-only scraper — REQ-013, Step 5.3.

* :func:`parse_markets_html` normalises a recorded HTML body into typed
  :class:`OutcomeQuote` rows (full markets, partial coverage missing the
  BTTS block, Cloudflare challenge).
* :class:`OneXBetClient` raises :class:`CloudflareBlocked` on a 403
  challenge response and surfaces parsed markets on a 200.
* :func:`record_market_availability` writes one row per ``(match_id,
  market, observed_at)`` and is idempotent on the natural key.
* :func:`probe` writes a markdown status report for each of the three
  outcomes (ok / cloudflare_blocked / error) without touching the DB.

No live HTTP — all fetches are respx-mocked off recorded fixtures.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import MarketAvailability, Match, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.odds.one_x_bet import (
    CloudflareBlocked,
    OneXBetClient,
    OneXBetError,
    parse_markets_html,
    probe,
    record_market_availability,
)

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PREDICTOR_ROOT / "tests" / "fixtures" / "one_x_bet"
MATCH_URL = "https://1xbet.com/en/line/football/12345-fifa-world-cup/mexico-vs-south-africa"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_markets_full_coverage() -> None:
    parsed = parse_markets_html(_load("match_full.html"))

    assert parsed.home_team == "Mexico"
    assert parsed.away_team == "South Africa"
    assert parsed.markets_present() == {"h2h", "totals_2.5", "btts", "corners_9.5"}

    by_key = {(q.market, q.outcome): q.decimal_odds for q in parsed.quotes}
    assert by_key[("h2h", "home")] == 2.40
    assert by_key[("h2h", "draw")] == 3.30
    assert by_key[("h2h", "away")] == 3.10
    assert by_key[("totals_2.5", "over")] == 2.05
    assert by_key[("totals_2.5", "under")] == 1.80
    assert by_key[("btts", "yes")] == 1.95
    assert by_key[("btts", "no")] == 1.90
    assert by_key[("corners_9.5", "over")] == 1.90
    assert by_key[("corners_9.5", "under")] == 1.95


def test_parse_markets_partial_coverage_skips_btts() -> None:
    parsed = parse_markets_html(_load("match_no_btts.html"))

    present = parsed.markets_present()
    assert "btts" not in present
    assert present == {"h2h", "totals_2.5", "corners_9.5"}
    # Other markets still come through cleanly.
    by_key = {(q.market, q.outcome): q.decimal_odds for q in parsed.quotes}
    assert by_key[("h2h", "home")] == 2.42
    assert by_key[("corners_9.5", "under")] == 1.93


def test_parse_markets_raises_on_cloudflare_body() -> None:
    with pytest.raises(CloudflareBlocked):
        parse_markets_html(_load("cloudflare_block.html"))


def test_parse_markets_raises_on_missing_json_island() -> None:
    with pytest.raises(OneXBetError, match=r"__INITIAL_STATE__"):
        parse_markets_html("<html><body>no island here</body></html>")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@respx.mock
def test_client_fetches_and_parses_full_markets() -> None:
    respx.get(MATCH_URL).mock(return_value=httpx.Response(200, text=_load("match_full.html")))
    with OneXBetClient() as client:
        parsed = client.fetch_match_markets(MATCH_URL)
    assert parsed.home_team == "Mexico"
    assert "btts" in parsed.markets_present()


@respx.mock
def test_client_raises_cloudflare_blocked_on_403_challenge() -> None:
    respx.get(MATCH_URL).mock(
        return_value=httpx.Response(
            403,
            text=_load("cloudflare_block.html"),
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        )
    )
    with OneXBetClient() as client, pytest.raises(CloudflareBlocked):
        client.fetch_match_markets(MATCH_URL)


@respx.mock
def test_client_raises_on_other_4xx() -> None:
    respx.get(MATCH_URL).mock(return_value=httpx.Response(404, text="not found"))
    with OneXBetClient() as client, pytest.raises(OneXBetError, match=r"status 404"):
        client.fetch_match_markets(MATCH_URL)


# ---------------------------------------------------------------------------
# record_market_availability
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PREDICTOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PREDICTOR_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("PREDICTOR_DB_URL", url)
    reset_settings_for_tests()
    reset_engines_for_tests()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    yield url
    reset_engines_for_tests()
    reset_settings_for_tests()


@pytest.fixture
def seeded_match(db_url: str) -> Iterator[int]:
    engine = get_engine()
    with Session(engine) as session:
        home = Team(name="Mexico", country="Mexico")
        away = Team(name="South Africa", country="South Africa")
        session.add_all([home, away])
        session.commit()
        assert home.id is not None and away.id is not None
        match = Match(
            competition="FIFA World Cup",
            season="2026",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=datetime(2026, 6, 11, 19, 0),
        )
        session.add(match)
        session.commit()
        assert match.id is not None
        yield match.id


def test_record_market_availability_writes_and_is_idempotent(seeded_match: int) -> None:
    observed = datetime(2026, 6, 5, 12, 0)
    engine = get_engine()
    with Session(engine) as session:
        first = record_market_availability(
            session,
            match_id=seeded_match,
            market="btts",
            available=False,
            reason="cloudflare_blocked",
            observed_at=observed,
        )
        session.commit()
        second = record_market_availability(
            session,
            match_id=seeded_match,
            market="btts",
            available=False,
            reason="cloudflare_blocked",
            observed_at=observed,
        )
        session.commit()
        rows = session.exec(
            select(MarketAvailability).where(MarketAvailability.match_id == seeded_match)
        ).all()

    assert first is not None
    assert second is None  # second insert is a no-op
    assert len(rows) == 1
    assert rows[0].market == "btts"
    assert rows[0].reason == "cloudflare_blocked"
    assert rows[0].available is False


# ---------------------------------------------------------------------------
# probe CLI
# ---------------------------------------------------------------------------


@respx.mock
def test_probe_writes_ok_report_on_success(tmp_path: Path) -> None:
    respx.get(MATCH_URL).mock(return_value=httpx.Response(200, text=_load("match_full.html")))
    out = tmp_path / "scraper-status.md"
    path = probe(url=MATCH_URL, out_path=out, now=datetime(2026, 6, 5, 12, 0))
    body = path.read_text(encoding="utf-8")
    assert "**Outcome**: ok" in body
    assert "Mexico vs South Africa" in body
    assert "btts" in body
    assert "corners_9.5" in body


@respx.mock
def test_probe_writes_cloudflare_report(tmp_path: Path) -> None:
    respx.get(MATCH_URL).mock(
        return_value=httpx.Response(
            403,
            text=_load("cloudflare_block.html"),
            headers={"server": "cloudflare"},
        )
    )
    out = tmp_path / "scraper-status.md"
    probe(url=MATCH_URL, out_path=out, now=datetime(2026, 6, 5, 12, 0))
    body = out.read_text(encoding="utf-8")
    assert "**Outcome**: cloudflare_blocked" in body


@respx.mock
def test_probe_writes_error_report_on_unparseable_body(tmp_path: Path) -> None:
    respx.get(MATCH_URL).mock(return_value=httpx.Response(200, text="<html>no island</html>"))
    out = tmp_path / "scraper-status.md"
    probe(url=MATCH_URL, out_path=out, now=datetime(2026, 6, 5, 12, 0))
    body = out.read_text(encoding="utf-8")
    assert "**Outcome**: error" in body
    assert "__INITIAL_STATE__" in body

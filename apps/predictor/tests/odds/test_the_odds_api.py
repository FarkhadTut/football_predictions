"""Tests for the the-odds-api client + persistence layer.

Covers Step 5.1 acceptance for REQ-012:

* respx-mocked HTTP responses parse into the typed event hierarchy
* quota headers are surfaced on the client
* non-2xx responses raise :class:`OddsApiError`
* ``persist_snapshots`` writes ``odds_snapshots`` rows for matched events
* re-running the persist with the same ``fetched_at`` is a no-op
* events that do not resolve against ``matches`` are surfaced for the caller

Match resolution and DB writes use the same alembic-up SQLite fixture
pattern as ``tests/ingest/test_tournaments.py``.
"""

from __future__ import annotations

import json
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
from predictor.db.models import Match, OddsSnapshot, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.odds.the_odds_api import (
    BASE_URL,
    SPORT_KEY_WC,
    OddsApiError,
    TheOddsApiClient,
    persist_snapshots,
)

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PREDICTOR_ROOT / "tests" / "fixtures" / "odds_api"


# ---------------------------------------------------------------------------
# Fixtures: alembic-up SQLite + seeded match
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


SEED_KICKOFF = datetime(2026, 6, 11, 19, 0)


@pytest.fixture
def seeded_db(db_url: str) -> Iterator[int]:
    """Seed Mexico + South Africa + their fixture match; return ``matches.id``."""
    engine = get_engine()
    with Session(engine) as session:
        mexico = Team(name="Mexico", country="Mexico")
        south_africa = Team(name="South Africa", country="South Africa")
        session.add_all([mexico, south_africa])
        session.commit()
        assert mexico.id is not None and south_africa.id is not None
        match = Match(
            competition="FIFA World Cup",
            season="2026",
            home_team_id=mexico.id,
            away_team_id=south_africa.id,
            kickoff_utc=SEED_KICKOFF,
        )
        session.add(match)
        session.commit()
        assert match.id is not None
        yield match.id


# ---------------------------------------------------------------------------
# Fixture payload loaders
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> list[dict[str, object]]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list)
    return data


def _single_event_payload(
    fixture_name: str, *, bookmakers_limit: int = 2
) -> list[dict[str, object]]:
    """Trim the live fixture to the first event + first N bookmakers."""
    events = _load_fixture(fixture_name)
    first = dict(events[0])
    bookmakers = first["bookmakers"]
    assert isinstance(bookmakers, list)
    first["bookmakers"] = bookmakers[:bookmakers_limit]
    return [first]


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_odds_parses_h2h_response() -> None:
    payload = _single_event_payload("h2h.json", bookmakers_limit=2)
    route = respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(
            200,
            json=payload,
            headers={"x-requests-remaining": "498", "x-requests-used": "2"},
        )
    )

    with TheOddsApiClient("test-key") as client:
        events = client.fetch_odds(markets=["h2h"], regions=["eu", "uk"])

    assert route.called
    sent = route.calls.last.request
    assert sent.url.params["apiKey"] == "test-key"
    assert sent.url.params["regions"] == "eu,uk"
    assert sent.url.params["markets"] == "h2h"
    assert sent.url.params["oddsFormat"] == "decimal"

    assert len(events) == 1
    event = events[0]
    assert event.home_team == "Mexico"
    assert event.away_team == "South Africa"
    assert len(event.bookmakers) == 2
    first_market = event.bookmakers[0].markets[0]
    assert first_market.key == "h2h"
    assert {o.name for o in first_market.outcomes} == {"Mexico", "South Africa", "Draw"}


@respx.mock
def test_fetch_odds_surfaces_quota_headers() -> None:
    respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": "123", "x-requests-used": "377"},
        )
    )
    client = TheOddsApiClient("test-key")
    try:
        client.fetch_odds(markets=["h2h"], regions=["eu"])
        assert client.requests_remaining == 123
        assert client.requests_used == 377
    finally:
        client.close()


@respx.mock
def test_fetch_odds_raises_on_non_2xx() -> None:
    respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )
    with TheOddsApiClient("bad-key") as client, pytest.raises(OddsApiError) as excinfo:
        client.fetch_odds(markets=["h2h"], regions=["eu"])
    assert excinfo.value.status_code == 401
    assert "invalid api key" in excinfo.value.detail


def test_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        TheOddsApiClient("")


def test_fetch_odds_validates_inputs() -> None:
    with TheOddsApiClient("k") as client:
        with pytest.raises(ValueError, match="markets"):
            client.fetch_odds(markets=[], regions=["eu"])
        with pytest.raises(ValueError, match="regions"):
            client.fetch_odds(markets=["h2h"], regions=[])


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


@respx.mock
def test_persist_snapshots_writes_h2h_rows(seeded_db: int) -> None:
    payload = _single_event_payload("h2h.json", bookmakers_limit=2)
    respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(200, json=payload)
    )
    match_id = seeded_db
    fetched_at = datetime(2026, 6, 2, 21, 0)

    with TheOddsApiClient("k") as client:
        events = client.fetch_odds(markets=["h2h"], regions=["eu", "uk"])
    engine = get_engine()
    with Session(engine) as session:
        result = persist_snapshots(session, events, fetched_at=fetched_at)
        session.commit()
        rows = session.exec(select(OddsSnapshot).where(OddsSnapshot.match_id == match_id)).all()

    # 2 bookmakers x 3 outcomes
    assert result.snapshots_added == 6
    assert result.snapshots_skipped_existing == 0
    assert result.events_unmatched == []
    assert len(rows) == 6
    assert {r.outcome for r in rows} == {"home", "draw", "away"}
    assert {r.book for r in rows} == {"paddypower", "skybet"}
    assert all(r.market == "h2h" for r in rows)


@respx.mock
def test_persist_snapshots_is_idempotent(seeded_db: int) -> None:
    payload = _single_event_payload("h2h.json", bookmakers_limit=1)
    respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(200, json=payload)
    )
    fetched_at = datetime(2026, 6, 2, 21, 0)

    with TheOddsApiClient("k") as client:
        events = client.fetch_odds(markets=["h2h"], regions=["eu"])

    engine = get_engine()
    with Session(engine) as session:
        first = persist_snapshots(session, events, fetched_at=fetched_at)
        session.commit()
        second = persist_snapshots(session, events, fetched_at=fetched_at)
        session.commit()
        total = session.exec(select(OddsSnapshot)).all()

    assert first.snapshots_added == 3
    assert second.snapshots_added == 0
    assert second.snapshots_skipped_existing == 3
    assert len(total) == 3


@respx.mock
def test_persist_snapshots_folds_totals_point_into_market_label(
    seeded_db: int,
) -> None:
    payload = _single_event_payload("totals.json", bookmakers_limit=2)
    respx.get(f"{BASE_URL}/sports/{SPORT_KEY_WC}/odds").mock(
        return_value=httpx.Response(200, json=payload)
    )
    match_id = seeded_db
    fetched_at = datetime(2026, 6, 2, 21, 5)

    with TheOddsApiClient("k") as client:
        events = client.fetch_odds(markets=["totals"], regions=["eu", "uk"])
    engine = get_engine()
    with Session(engine) as session:
        result = persist_snapshots(session, events, fetched_at=fetched_at)
        session.commit()
        rows = session.exec(select(OddsSnapshot).where(OddsSnapshot.match_id == match_id)).all()

    # 2 bookmakers x 2 outcomes (Over/Under)
    assert result.snapshots_added == 4
    markets = {r.market for r in rows}
    assert markets == {"totals_2.25", "totals_2.5"}
    assert {r.outcome for r in rows} == {"over", "under"}


def test_persist_snapshots_records_unmatched_event(db_url: str) -> None:
    """Event whose teams / kickoff aren't in the DB yet is surfaced, not raised."""
    from predictor.odds.the_odds_api import OddsApiBookmaker, OddsApiEvent

    engine = get_engine()
    event = OddsApiEvent(
        id="unmatched-event-1",
        sport_key=SPORT_KEY_WC,
        sport_title="FIFA World Cup",
        commence_time=datetime(2026, 7, 1, 18, 0),
        home_team="Nowhere FC",
        away_team="Phantom United",
        bookmakers=[
            OddsApiBookmaker.model_validate(
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-06-02T21:00:00Z",
                    "markets": [],
                }
            )
        ],
    )
    with Session(engine) as session:
        result = persist_snapshots(session, [event], fetched_at=datetime(2026, 6, 2, 21, 0))
        session.commit()

    assert result.snapshots_added == 0
    assert result.events_unmatched == ["unmatched-event-1"]

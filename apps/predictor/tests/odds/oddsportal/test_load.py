"""Loader tests: fuzzy resolution, home/away orientation, idempotency.

Uses the same alembic-up SQLite fixture pattern as
``tests/odds/test_the_odds_api.py``. The source is faked with ``OddsRow``s, so
no browser/network is involved.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, OddsSnapshot, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.odds.oddsportal.contracts import OddsRow
from predictor.odds.oddsportal.load import load_oddsportal

PREDICTOR_ROOT = Path(__file__).resolve().parents[3]
COMP = "INT-World Cup"
SEASON = "2018"
FETCHED_AT = datetime(2026, 6, 14)


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PREDICTOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PREDICTOR_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Seed France (home) vs Croatia (away), 2018-07-15. Yields match id."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("PREDICTOR_DB_URL", url)
    reset_settings_for_tests()
    reset_engines_for_tests()
    command.upgrade(_alembic_config(url), "head")
    with Session(get_engine()) as session:
        france = Team(name="France")
        croatia = Team(name="Croatia")
        session.add_all([france, croatia])
        session.commit()
        assert france.id is not None and croatia.id is not None
        match = Match(
            competition=COMP,
            season=SEASON,
            home_team_id=france.id,
            away_team_id=croatia.id,
            kickoff_utc=datetime(2018, 7, 15, 18, 0),
        )
        session.add(match)
        session.commit()
        assert match.id is not None
        yield match.id
    reset_engines_for_tests()
    reset_settings_for_tests()


class FakeSource:
    """Minimal stand-in exposing ``fetch_tournament_odds``."""

    def __init__(self, rows: list[OddsRow]) -> None:
        self._rows = rows

    def fetch_tournament_odds(self, name: str) -> list[OddsRow]:
        return list(self._rows)


def _h2h(home: str, away: str, h: float, d: float, a: float, *, on: date) -> list[OddsRow]:
    common = {
        "competition": COMP,
        "season": SEASON,
        "home_team": home,
        "away_team": away,
        "match_date": on,
        "market": "h2h",
    }
    return [
        OddsRow(**common, outcome="home", decimal_odds=h),
        OddsRow(**common, outcome="draw", decimal_odds=d),
        OddsRow(**common, outcome="away", decimal_odds=a),
    ]


def _rows_for(session: Session, match_id: int) -> dict[str, float]:
    snaps = session.exec(select(OddsSnapshot).where(OddsSnapshot.match_id == match_id)).all()
    return {s.outcome: s.decimal_odds for s in snaps}


def test_load_writes_h2h_rows(seeded_db: int) -> None:
    src = FakeSource(_h2h("France", "Croatia", 2.02, 2.98, 4.88, on=date(2018, 7, 15)))
    with Session(get_engine()) as session:
        result = load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
        odds = _rows_for(session, seeded_db)

    assert (result.matches_resolved, result.matches_unmatched, result.rows_added) == (1, 0, 3)
    assert odds == {"home": 2.02, "draw": 2.98, "away": 4.88}
    with Session(get_engine()) as session:
        books = {s.book for s in session.exec(select(OddsSnapshot)).all()}
    assert books == {"oddsportal"}


def test_load_orients_swapped_home_away(seeded_db: int) -> None:
    # OddsPortal lists Croatia as home; DB has France as home -> outcomes flip.
    src = FakeSource(_h2h("Croatia", "France", 3.0, 3.2, 1.5, on=date(2018, 7, 15)))
    with Session(get_engine()) as session:
        load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
        odds = _rows_for(session, seeded_db)

    # DB home == France == OddsPortal away (1.5); DB away == Croatia (3.0); draw unchanged.
    assert odds == {"home": 1.5, "draw": 3.2, "away": 3.0}


def test_load_resolves_despite_wrong_date(seeded_db: int) -> None:
    # OddsPortal date off by 5 days (timezone / phantom) still resolves by pair.
    src = FakeSource(_h2h("France", "Croatia", 2.0, 3.0, 4.0, on=date(2018, 7, 20)))
    with Session(get_engine()) as session:
        result = load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
    assert (result.matches_resolved, result.matches_unmatched) == (1, 0)


def test_load_is_idempotent(seeded_db: int) -> None:
    src = FakeSource(_h2h("France", "Croatia", 2.02, 2.98, 4.88, on=date(2018, 7, 15)))
    with Session(get_engine()) as session:
        first = load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
        second = load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
        total = session.exec(select(OddsSnapshot)).all()
    assert first.rows_added == 3
    assert (second.rows_added, second.rows_skipped_existing) == (0, 3)
    assert len(total) == 3


def test_load_records_unmatched(seeded_db: int) -> None:
    src = FakeSource(_h2h("Narnia", "France", 2.0, 3.0, 4.0, on=date(2018, 7, 15)))
    with Session(get_engine()) as session:
        result = load_oddsportal(session, src, "WC 2018", fetched_at=FETCHED_AT)  # type: ignore[arg-type]
        total = session.exec(select(OddsSnapshot)).all()
    assert (result.matches_resolved, result.matches_unmatched, result.rows_added) == (0, 1, 0)
    assert total == []

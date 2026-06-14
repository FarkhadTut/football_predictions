"""Tests for merging coded team rows into their canonical full-name row."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.ingest.team_dedup import merge_team_codes

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PREDICTOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PREDICTOR_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    db_url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("PREDICTOR_DB_URL", db_url)
    reset_settings_for_tests()
    reset_engines_for_tests()
    command.upgrade(_alembic_config(db_url), "head")
    with Session(get_engine()) as s:
        yield s
    reset_engines_for_tests()
    reset_settings_for_tests()


def _team(session: Session, name: str) -> int:
    t = Team(name=name)
    session.add(t)
    session.commit()
    session.refresh(t)
    assert t.id is not None
    return t.id


def test_merge_repoints_match_and_deletes_coded_team(session: Session) -> None:
    bra = _team(session, "BRA")
    arg = _team(session, "ARG")
    brazil = _team(session, "Brazil")
    argentina = _team(session, "Argentina")
    m = Match(
        competition="WC",
        season="2026",
        home_team_id=bra,
        away_team_id=arg,
        kickoff_utc=datetime(2026, 6, 11, 19, 0),
    )
    session.add(m)
    session.commit()

    result = merge_team_codes(session, {"BRA": "Brazil", "ARG": "Argentina"})

    assert result.teams_deleted == 2
    assert result.matches_repointed == 1
    assert set(result.merged) == {("BRA", "Brazil"), ("ARG", "Argentina")}
    # Coded rows gone.
    assert session.exec(select(Team).where(Team.name.in_(("BRA", "ARG")))).all() == []  # type: ignore[attr-defined]
    # Match now points at the canonical teams.
    refreshed = session.exec(select(Match)).one()
    assert (refreshed.home_team_id, refreshed.away_team_id) == (brazil, argentina)


def test_merge_is_idempotent(session: Session) -> None:
    _team(session, "Brazil")
    bra = _team(session, "BRA")
    session.add(
        Match(
            competition="WC",
            season="2026",
            home_team_id=bra,
            away_team_id=_team(session, "Argentina"),
            kickoff_utc=datetime(2026, 6, 11, 19, 0),
        )
    )
    session.commit()

    first = merge_team_codes(session, {"BRA": "Brazil"})
    second = merge_team_codes(session, {"BRA": "Brazil"})
    assert first.teams_deleted == 1
    assert second.teams_deleted == 0
    assert second.merged == []


def test_merge_renames_when_no_canonical_row(session: Session) -> None:
    _team(session, "FRA")
    result = merge_team_codes(session, {"FRA": "France"})
    assert result.renamed == [("FRA", "France")]
    assert result.teams_deleted == 0
    assert session.exec(select(Team).where(Team.name == "France")).first() is not None
    assert session.exec(select(Team).where(Team.name == "FRA")).first() is None


def test_merge_drops_duplicate_match_instead_of_violating_key(session: Session) -> None:
    bra = _team(session, "BRA")
    brazil = _team(session, "Brazil")
    france = _team(session, "France")
    kickoff = datetime(2026, 6, 14, 18, 0)
    # A coded match and its canonical twin (same natural key after repoint).
    session.add(
        Match(competition="WC", season="2026", home_team_id=bra, away_team_id=france, kickoff_utc=kickoff)
    )
    session.add(
        Match(
            competition="WC",
            season="2026",
            home_team_id=brazil,
            away_team_id=france,
            kickoff_utc=kickoff,
        )
    )
    session.commit()

    result = merge_team_codes(session, {"BRA": "Brazil"})
    assert result.matches_deleted_as_dupe == 1
    assert result.matches_repointed == 0
    # Exactly one match remains, on the canonical team.
    remaining = session.exec(select(Match)).all()
    assert len(remaining) == 1
    assert remaining[0].home_team_id == brazil

"""Tests for the recent-club-matches loader."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, MatchStat, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.ingest.clubs import LoadResult, load_recent_club_matches
from predictor.ingest.contracts import ScheduleRow, TeamMatchStatRow

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]


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


@dataclass
class FakeClubSource:
    schedule: list[ScheduleRow] = field(default_factory=list)
    stats: list[TeamMatchStatRow] = field(default_factory=list)
    seen_player_ids: list[list[str]] = field(default_factory=list)

    def fetch_schedule(self, player_fbref_ids: list[str], since: datetime) -> list[ScheduleRow]:
        self.seen_player_ids.append(list(player_fbref_ids))
        return list(self.schedule)

    def fetch_team_match_stats(
        self, player_fbref_ids: list[str], since: datetime
    ) -> list[TeamMatchStatRow]:
        return list(self.stats)


def _fixture_source() -> FakeClubSource:
    """One match in EPL, one in La Liga; stats for the EPL match only."""
    m1 = datetime(2025, 9, 14, 14, 0)
    m2 = datetime(2025, 10, 26, 19, 0)
    schedule = [
        ScheduleRow(
            competition="ENG-Premier League",
            season="2025-2026",
            home_team="Arsenal",
            away_team="Manchester United",
            kickoff_utc=m1,
            home_goals=3,
            away_goals=1,
        ),
        ScheduleRow(
            competition="ESP-La Liga",
            season="2025-2026",
            home_team="Real Madrid",
            away_team="Barcelona",
            kickoff_utc=m2,
            home_goals=2,
            away_goals=2,
        ),
    ]
    stats = [
        TeamMatchStatRow(
            competition="ENG-Premier League",
            season="2025-2026",
            home_team="Arsenal",
            away_team="Manchester United",
            kickoff_utc=m1,
            team="Arsenal",
            shots=18,
            shots_on_target=7,
            corners=8,
            yellow_cards=1,
            red_cards=0,
            fouls=9,
        ),
        TeamMatchStatRow(
            competition="ENG-Premier League",
            season="2025-2026",
            home_team="Arsenal",
            away_team="Manchester United",
            kickoff_utc=m1,
            team="Manchester United",
            shots=10,
            shots_on_target=3,
            corners=4,
            yellow_cards=3,
            red_cards=0,
            fouls=14,
        ),
    ]
    return FakeClubSource(schedule=schedule, stats=stats)


SINCE = datetime(2023, 7, 1)
CANDIDATE_IDS = ["p-vini", "p-rodri", "p-saka"]


def test_first_load_inserts_club_matches_and_passes_player_ids(db_url: str) -> None:
    source = _fixture_source()
    with Session(get_engine()) as session:
        result = load_recent_club_matches(session, source, CANDIDATE_IDS, SINCE)

    assert result == LoadResult(
        teams_added=4,
        matches_added=2,
        matches_updated=0,
        stats_added=2,
        stats_updated=0,
    )
    # Loader forwards the candidate pool to the source verbatim.
    assert source.seen_player_ids == [CANDIDATE_IDS]

    with Session(get_engine()) as session:
        teams = {t.name for t in session.exec(select(Team)).all()}
        assert teams == {"Arsenal", "Manchester United", "Real Madrid", "Barcelona"}

        matches = session.exec(select(Match)).all()
        assert {m.competition for m in matches} == {
            "ENG-Premier League",
            "ESP-La Liga",
        }
        assert all(m.status == "final" for m in matches)
        assert len(session.exec(select(MatchStat)).all()) == 2


def test_reload_is_idempotent(db_url: str) -> None:
    source = _fixture_source()
    with Session(get_engine()) as session:
        load_recent_club_matches(session, source, CANDIDATE_IDS, SINCE)
    with Session(get_engine()) as session:
        result = load_recent_club_matches(session, source, CANDIDATE_IDS, SINCE)

    assert result == LoadResult(
        teams_added=0,
        matches_added=0,
        matches_updated=0,
        stats_added=0,
        stats_updated=0,
    )
    with Session(get_engine()) as session:
        assert len(session.exec(select(Match)).all()) == 2
        assert len(session.exec(select(Team)).all()) == 4

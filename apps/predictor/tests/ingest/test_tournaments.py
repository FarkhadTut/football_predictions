"""Unit tests for the tournament loader.

We sidestep ``soccerdata`` by injecting a ``FakeSource`` that returns
fixture rows directly — the loader contract is the ``TournamentSource``
Protocol, not the FBref HTTP layer. This is more maintainable than
mocking ``soccerdata``'s disk-cached, rate-limited HTTP calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, MatchStat, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.ingest.tournaments import (
    LoadResult,
    ScheduleRow,
    TeamMatchStatRow,
    fbref_league_for,
    load_tournament,
)

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


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


@dataclass
class FakeSource:
    schedule: list[ScheduleRow] = field(default_factory=list)
    stats: list[TeamMatchStatRow] = field(default_factory=list)

    def fetch_schedule(self, name: str, season: str) -> list[ScheduleRow]:
        return list(self.schedule)

    def fetch_team_match_stats(self, name: str, season: str) -> list[TeamMatchStatRow]:
        return list(self.stats)


def _euro_2024_fixture() -> FakeSource:
    """Three matches across Euro 2024 group + knockout, with paired team stats."""
    common = {"competition": "Euro 2024", "season": "2024"}
    m1_kick = datetime(2024, 6, 15, 19, 0)
    m2_kick = datetime(2024, 6, 16, 16, 0)
    m3_kick = datetime(2024, 7, 5, 20, 0)

    schedule = [
        ScheduleRow(
            **common,
            home_team="Spain",
            away_team="France",
            kickoff_utc=m1_kick,
            home_goals=2,
            away_goals=1,
        ),
        ScheduleRow(
            **common,
            home_team="Germany",
            away_team="Italy",
            kickoff_utc=m2_kick,
            home_goals=1,
            away_goals=1,
        ),
        ScheduleRow(
            **common,
            home_team="Spain",
            away_team="Germany",
            kickoff_utc=m3_kick,
            home_goals=None,
            away_goals=None,
        ),
    ]

    def stat(
        kickoff: datetime,
        home: str,
        away: str,
        team: str,
        *,
        shots: int,
        sot: int,
        corners: int,
        yel: int,
        red: int,
        fouls: int,
    ) -> TeamMatchStatRow:
        return TeamMatchStatRow(
            competition="Euro 2024",
            season="2024",
            home_team=home,
            away_team=away,
            kickoff_utc=kickoff,
            team=team,
            shots=shots,
            shots_on_target=sot,
            corners=corners,
            yellow_cards=yel,
            red_cards=red,
            fouls=fouls,
        )

    stats = [
        stat(
            m1_kick, "Spain", "France", "Spain", shots=14, sot=6, corners=7, yel=2, red=0, fouls=11
        ),
        stat(
            m1_kick, "Spain", "France", "France", shots=9, sot=3, corners=4, yel=3, red=0, fouls=14
        ),
        stat(
            m2_kick,
            "Germany",
            "Italy",
            "Germany",
            shots=12,
            sot=5,
            corners=6,
            yel=1,
            red=0,
            fouls=10,
        ),
        stat(
            m2_kick, "Germany", "Italy", "Italy", shots=8, sot=2, corners=3, yel=2, red=0, fouls=12
        ),
    ]

    return FakeSource(schedule=schedule, stats=stats)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_fbref_league_for_known_tournaments() -> None:
    assert fbref_league_for("Euro 2024") == "INT-European Championship"
    assert fbref_league_for("WC 2022") == "INT-World Cup"


def test_fbref_league_for_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown tournament"):
        fbref_league_for("Bogus 2099")


# ---------------------------------------------------------------------------
# Loader behavior
# ---------------------------------------------------------------------------


def test_first_load_inserts_teams_matches_and_stats(db_url: str) -> None:
    source = _euro_2024_fixture()
    with Session(get_engine()) as session:
        result = load_tournament(session, source, "Euro 2024", "2024")

    assert result == LoadResult(
        teams_added=4, matches_added=3, matches_updated=0, stats_added=4, stats_updated=0
    )

    with Session(get_engine()) as session:
        teams = {t.name for t in session.exec(select(Team)).all()}
        assert teams == {"Spain", "France", "Germany", "Italy"}

        matches = session.exec(select(Match)).all()
        assert len(matches) == 3
        finalised = [m for m in matches if m.status == "final"]
        scheduled = [m for m in matches if m.status == "scheduled"]
        assert len(finalised) == 2
        assert len(scheduled) == 1
        assert scheduled[0].home_goals is None and scheduled[0].away_goals is None

        stats_rows = session.exec(select(MatchStat)).all()
        assert len(stats_rows) == 4
        # The Spain row from match 1 should report 7 corners.
        spain = session.exec(select(Team).where(Team.name == "Spain")).one()
        m1 = next(m for m in matches if m.kickoff_utc == datetime(2024, 6, 15, 19, 0))
        spain_stat = next(s for s in stats_rows if s.match_id == m1.id and s.team_id == spain.id)
        assert spain_stat.corners == 7
        assert spain_stat.shots == 14


def test_reload_is_idempotent(db_url: str) -> None:
    source = _euro_2024_fixture()
    with Session(get_engine()) as session:
        load_tournament(session, source, "Euro 2024", "2024")
    with Session(get_engine()) as session:
        result = load_tournament(session, source, "Euro 2024", "2024")

    assert result == LoadResult(
        teams_added=0, matches_added=0, matches_updated=0, stats_added=0, stats_updated=0
    )

    with Session(get_engine()) as session:
        assert len(session.exec(select(Match)).all()) == 3
        assert len(session.exec(select(MatchStat)).all()) == 4
        assert len(session.exec(select(Team)).all()) == 4


def test_reload_with_updated_score_updates_match(db_url: str) -> None:
    source = _euro_2024_fixture()
    with Session(get_engine()) as session:
        load_tournament(session, source, "Euro 2024", "2024")

    # Now the final match has a result; re-fetch should update it in place.
    updated_schedule = list(source.schedule)
    final_match = updated_schedule[-1]
    updated_schedule[-1] = replace(final_match, home_goals=2, away_goals=1)
    source_v2 = FakeSource(schedule=updated_schedule, stats=source.stats)

    with Session(get_engine()) as session:
        result = load_tournament(session, source_v2, "Euro 2024", "2024")

    assert result.matches_added == 0
    assert result.matches_updated == 1
    assert result.stats_added == 0
    assert result.stats_updated == 0

    with Session(get_engine()) as session:
        m3 = session.exec(
            select(Match).where(Match.kickoff_utc == datetime(2024, 7, 5, 20, 0))
        ).one()
        assert m3.home_goals == 2 and m3.away_goals == 1
        assert m3.status == "final"


def test_reload_with_changed_stats_updates_stats(db_url: str) -> None:
    source = _euro_2024_fixture()
    with Session(get_engine()) as session:
        load_tournament(session, source, "Euro 2024", "2024")

    # Bump Spain's corners from 7 to 9.
    updated_stats = list(source.stats)
    spain_idx = next(i for i, s in enumerate(updated_stats) if s.team == "Spain" and s.corners == 7)
    updated_stats[spain_idx] = replace(updated_stats[spain_idx], corners=9)
    source_v2 = FakeSource(schedule=source.schedule, stats=updated_stats)

    with Session(get_engine()) as session:
        result = load_tournament(session, source_v2, "Euro 2024", "2024")

    assert result.stats_updated == 1
    assert result.stats_added == 0

    with Session(get_engine()) as session:
        spain = session.exec(select(Team).where(Team.name == "Spain")).one()
        m1 = session.exec(
            select(Match).where(Match.kickoff_utc == datetime(2024, 6, 15, 19, 0))
        ).one()
        spain_stat = session.exec(
            select(MatchStat).where(MatchStat.match_id == m1.id, MatchStat.team_id == spain.id)
        ).one()
        assert spain_stat.corners == 9


def test_stats_referencing_unknown_match_are_skipped(db_url: str) -> None:
    # Stat row whose natural key matches no schedule row — loader should skip,
    # not crash.
    schedule_row = ScheduleRow(
        competition="Euro 2024",
        season="2024",
        home_team="Spain",
        away_team="France",
        kickoff_utc=datetime(2024, 6, 15, 19, 0),
        home_goals=2,
        away_goals=1,
    )
    orphan_stat = TeamMatchStatRow(
        competition="Euro 2024",
        season="2024",
        home_team="Spain",
        away_team="Portugal",  # not in schedule
        kickoff_utc=datetime(2024, 6, 15, 19, 0),
        team="Spain",
        shots=10,
    )
    source = FakeSource(schedule=[schedule_row], stats=[orphan_stat])

    with Session(get_engine()) as session:
        result = load_tournament(session, source, "Euro 2024", "2024")

    assert result.matches_added == 1
    assert result.stats_added == 0

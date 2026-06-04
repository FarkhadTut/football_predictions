"""Migration round-trip test + per-entity CRUD smoke.

Strategy:
- Each test points ``PREDICTOR_DB_URL`` at a fresh on-disk SQLite file inside
  a ``tmp_path`` (alembic + StaticPool don't compose well with ``:memory:``
  across separate connections).
- ``alembic upgrade head`` then ``downgrade base`` proves the schema can roll
  forward and back cleanly.
- A second test re-upgrades, inserts one row per entity, and reads it back to
  confirm the runtime ORM agrees with the migrated schema.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import (
    ClaudeNote,
    MarketAvailability,
    Match,
    MatchStat,
    ModelRun,
    OddsSnapshot,
    Player,
    Prediction,
    ScoreDistribution,
    Team,
    WCSquad,
)
from predictor.db.session import get_engine, reset_engines_for_tests

# Repository root for resolving alembic.ini relative to this test file.
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
    yield url
    reset_engines_for_tests()
    reset_settings_for_tests()


def test_upgrade_then_downgrade_roundtrip(db_url: str) -> None:
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")

    engine = get_engine()
    inspector = inspect(engine)
    expected_tables = {
        "teams",
        "players",
        "wc_squads",
        "matches",
        "match_stats",
        "odds_snapshots",
        "market_availability",
        "predictions",
        "score_distributions",
        "model_runs",
        "claude_notes",
    }
    assert expected_tables.issubset(set(inspector.get_table_names()))

    command.downgrade(cfg, "base")

    inspector = inspect(engine)
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {remaining}"


def test_crud_per_entity_after_upgrade(db_url: str) -> None:
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    now = datetime.now(UTC).replace(tzinfo=None)
    matrix = np.zeros((10, 10), dtype=np.float64)
    matrix[1, 0] = 0.5
    matrix[2, 1] = 0.5
    buf = io.BytesIO()
    np.save(buf, matrix, allow_pickle=False)
    matrix_bytes = buf.getvalue()

    with Session(get_engine()) as session:
        home = Team(name="Brazil", country="BR", fbref_id="t-bra")
        away = Team(name="Argentina", country="AR", fbref_id="t-arg")
        session.add(home)
        session.add(away)
        session.commit()
        session.refresh(home)
        session.refresh(away)
        assert home.id is not None and away.id is not None

        player = Player(name="Vinicius", nation="Brazil", fbref_id="p-vini", position="LW")
        session.add(player)
        session.commit()
        session.refresh(player)
        assert player.id is not None

        squad = WCSquad(nation="Brazil", player_id=player.id, source="heuristic", as_of_date=now)
        session.add(squad)

        match = Match(
            competition="WC",
            season="2026",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=now,
            status="scheduled",
        )
        session.add(match)
        session.commit()
        session.refresh(match)
        assert match.id is not None

        session.add(MatchStat(match_id=match.id, team_id=home.id, shots=15, corners=7))
        session.add(
            OddsSnapshot(
                match_id=match.id,
                book="pinnacle",
                market="h2h",
                outcome="home",
                decimal_odds=1.85,
                fetched_at=now,
            )
        )
        session.add(
            MarketAvailability(
                match_id=match.id,
                market="btts",
                available=False,
                reason="cloudflare_blocked",
                observed_at=now,
            )
        )

        run = ModelRun(
            model_version="dc-0.1.0",
            git_sha="deadbeef",
            training_cutoff_utc=now,
            fitter_config_json={"half_life_days": 365, "rho": -0.1},
            created_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None

        session.add(
            Prediction(
                match_id=match.id,
                market="h2h",
                outcome="home",
                probability=0.52,
                model_run_id=run.id,
                computed_at=now,
            )
        )
        session.add(
            ScoreDistribution(
                match_id=match.id,
                model_run_id=run.id,
                matrix=matrix_bytes,
                computed_at=now,
            )
        )
        session.add(
            ClaudeNote(
                match_id=match.id,
                path="notes/2026-06-14-bra-vs-arg.md",
                content_hash="0" * 64,
                ingested_at=now,
            )
        )
        session.commit()

    # Re-open a fresh session and read every entity back.
    with Session(get_engine()) as session:
        assert session.exec(select(Team).where(Team.name == "Brazil")).one().fbref_id == "t-bra"
        assert session.exec(select(Player)).one().name == "Vinicius"
        assert session.exec(select(WCSquad)).one().source == "heuristic"
        assert session.exec(select(Match)).one().competition == "WC"
        assert session.exec(select(MatchStat)).one().corners == 7
        assert session.exec(select(OddsSnapshot)).one().decimal_odds == pytest.approx(1.85)
        avail = session.exec(select(MarketAvailability)).one()
        assert avail.available is False and avail.reason == "cloudflare_blocked"
        run_row = session.exec(select(ModelRun)).one()
        assert run_row.fitter_config_json["rho"] == pytest.approx(-0.1)
        assert session.exec(select(Prediction)).one().probability == pytest.approx(0.52)
        dist_row = session.exec(select(ScoreDistribution)).one()
        round_tripped = np.load(io.BytesIO(dist_row.matrix), allow_pickle=False)
        assert round_tripped.shape == (10, 10)
        assert round_tripped[1, 0] == pytest.approx(0.5)
        assert session.exec(select(ClaudeNote)).one().content_hash == "0" * 64

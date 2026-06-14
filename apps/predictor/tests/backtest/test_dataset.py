"""Tests for the backtest dataset adapter (REQ-007).

The adapter is the only DB-touching layer of the backtest pipeline. These
tests exercise it against an in-memory schema seeded with a small
multi-tournament corpus so we verify:

* training corpus excludes held-out competition/season pairs
* baseline_probs come from the latest pre-kickoff OddsSnapshot per market,
  de-vigged via 1/odds normalisation
* baseline_probs fall back to empirical base rates when odds are absent
* corner Poisson λs come from training-set team corner means and
  total_corners is the sum of the per-team MatchStat rows
* CLI wiring: load_training_matches / load_test_matches feed
  run_walk_forward → check() without raising
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session

from predictor.backtest.dataset import (
    HELD_OUT_TOURNAMENTS,
    load_test_matches,
    load_training_matches,
)
from predictor.backtest.run import run_walk_forward
from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, MatchStat, OddsSnapshot, Team
from predictor.db.session import get_engine, reset_engines_for_tests

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]

# Explicit held-out set for these tests, independent of the production default
# in ``dataset.HELD_OUT_TOURNAMENTS``. Fixtures use WC 2018 as a training-anchor
# tournament and WC 2022 as the held-out test tournament, so hold out only the
# latter here.
_TH: tuple[tuple[str, str], ...] = (("INT-World Cup", "2022"),)


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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _team(session: Session, name: str) -> Team:
    t = Team(name=name)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _match(
    session: Session,
    *,
    competition: str,
    season: str,
    home: Team,
    away: Team,
    kickoff: datetime,
    home_goals: int | None,
    away_goals: int | None,
    home_corners: int | None = None,
    away_corners: int | None = None,
) -> Match:
    assert home.id is not None and away.id is not None
    m = Match(
        competition=competition,
        season=season,
        home_team_id=home.id,
        away_team_id=away.id,
        kickoff_utc=kickoff,
        home_goals=home_goals,
        away_goals=away_goals,
        status="final" if home_goals is not None else "scheduled",
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    assert m.id is not None
    if home_corners is not None:
        session.add(MatchStat(match_id=m.id, team_id=home.id, corners=home_corners))
    if away_corners is not None:
        session.add(MatchStat(match_id=m.id, team_id=away.id, corners=away_corners))
    session.commit()
    return m


def _odds(
    session: Session,
    *,
    match_id: int,
    market: str,
    outcome: str,
    decimal_odds: float,
    fetched_at: datetime,
    book: str = "pinnacle",
) -> None:
    session.add(
        OddsSnapshot(
            match_id=match_id,
            book=book,
            market=market,
            outcome=outcome,
            decimal_odds=decimal_odds,
            fetched_at=fetched_at,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# load_training_matches
# ---------------------------------------------------------------------------


def test_load_training_matches_excludes_held_out_tournaments(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=datetime(2018, 6, 1),
        home_goals=2,
        away_goals=1,
    )
    # Held-out — must NOT appear in training.
    _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 11, 21),
        home_goals=1,
        away_goals=1,
    )

    training = load_training_matches(session, held_out=_TH)
    assert len(training) == 1
    assert set(training.columns) >= {
        "home_team",
        "away_team",
        "home_goals",
        "away_goals",
        "kickoff_utc",
    }
    assert training.iloc[0]["home_goals"] == 2


def test_load_training_matches_skips_unfinished_matches(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    _match(
        session,
        competition="INT-European Championship",
        season="2016",
        home=a,
        away=b,
        kickoff=datetime(2016, 6, 1),
        home_goals=None,
        away_goals=None,
    )
    assert load_training_matches(session, held_out=_TH).empty


def test_load_training_matches_sorted_by_kickoff(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    base = datetime(2018, 6, 1)
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=base + timedelta(days=2),
        home_goals=1,
        away_goals=0,
    )
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=b,
        away=a,
        kickoff=base,
        home_goals=0,
        away_goals=3,
    )
    df = load_training_matches(session, held_out=_TH)
    assert list(df["kickoff_utc"]) == sorted(df["kickoff_utc"])


# ---------------------------------------------------------------------------
# load_test_matches
# ---------------------------------------------------------------------------


def test_load_test_matches_uses_de_vigged_odds_for_baseline(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    # One older training match to anchor empirical fallback.
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=datetime(2018, 6, 1),
        home_goals=2,
        away_goals=1,
    )
    # Held-out test match with odds.
    m = _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 11, 21),
        home_goals=1,
        away_goals=0,
    )
    assert m.id is not None
    # Implied: home 0.5, draw 0.25, away 0.25 → after de-vig (sum 1.0).
    pre_kick = datetime(2022, 11, 20)
    _odds(session, match_id=m.id, market="h2h", outcome="home", decimal_odds=2.0, fetched_at=pre_kick)
    _odds(session, match_id=m.id, market="h2h", outcome="draw", decimal_odds=4.0, fetched_at=pre_kick)
    _odds(session, match_id=m.id, market="h2h", outcome="away", decimal_odds=4.0, fetched_at=pre_kick)

    test_matches = load_test_matches(session, held_out=_TH)
    assert len(test_matches) == 1
    tm = test_matches[0]
    np.testing.assert_allclose(tm.baseline_probs["1x2"], [0.5, 0.25, 0.25], atol=1e-9)
    assert tm.home_team == "A"
    assert tm.tournament_id == "INT-World Cup|2022"


def test_load_test_matches_prefers_latest_odds_snapshot_per_outcome(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=datetime(2018, 6, 1),
        home_goals=1,
        away_goals=0,
    )
    m = _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 11, 21),
        home_goals=2,
        away_goals=2,
    )
    assert m.id is not None
    # Stale + fresh snapshots: fresh ones (1.0 for all → uniform) should win.
    stale = datetime(2022, 11, 1)
    fresh = datetime(2022, 11, 20)
    for outcome in ("home", "draw", "away"):
        _odds(session, match_id=m.id, market="h2h", outcome=outcome, decimal_odds=99.0, fetched_at=stale)
        _odds(session, match_id=m.id, market="h2h", outcome=outcome, decimal_odds=3.0, fetched_at=fresh)

    [tm] = load_test_matches(session, held_out=_TH)
    np.testing.assert_allclose(tm.baseline_probs["1x2"], [1 / 3, 1 / 3, 1 / 3], atol=1e-9)


def test_load_test_matches_falls_back_to_empirical_when_odds_missing(
    session: Session,
) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    # Training: 3 home wins, 1 away win, 0 draws → empirical 1x2 = (0.75, 0, 0.25).
    base = datetime(2018, 6, 1)
    for i, (hg, ag) in enumerate([(2, 0), (1, 0), (3, 1), (0, 2)]):
        _match(
            session,
            competition="INT-World Cup",
            season="2018",
            home=a,
            away=b,
            kickoff=base + timedelta(days=i),
            home_goals=hg,
            away_goals=ag,
        )
    # Held-out test match with NO odds.
    _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 11, 21),
        home_goals=1,
        away_goals=0,
    )
    [tm] = load_test_matches(session, held_out=_TH)
    np.testing.assert_allclose(tm.baseline_probs["1x2"], [0.75, 0.0, 0.25], atol=1e-9)
    # All required markets must be populated so acceptance.check() doesn't raise.
    assert set(tm.baseline_probs) >= {"1x2", "ou_2_5", "btts"}


def test_load_test_matches_skips_unfinished_held_out_matches(session: Session) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 12, 1),
        home_goals=None,
        away_goals=None,
    )
    assert load_test_matches(session, held_out=_TH) == []


def test_load_test_matches_includes_corner_lambdas_when_stats_present(
    session: Session,
) -> None:
    a, b = _team(session, "A"), _team(session, "B")
    # Training corner means: A=8 (over 2 matches), B=4.
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=datetime(2018, 6, 1),
        home_goals=1,
        away_goals=0,
        home_corners=10,
        away_corners=4,
    )
    _match(
        session,
        competition="INT-World Cup",
        season="2018",
        home=a,
        away=b,
        kickoff=datetime(2018, 6, 5),
        home_goals=2,
        away_goals=1,
        home_corners=6,
        away_corners=4,
    )
    # Held-out test match with corner observation.
    _match(
        session,
        competition="INT-World Cup",
        season="2022",
        home=a,
        away=b,
        kickoff=datetime(2022, 11, 21),
        home_goals=2,
        away_goals=1,
        home_corners=7,
        away_corners=5,
    )

    [tm] = load_test_matches(session, held_out=_TH)
    assert tm.corner_lambdas is not None
    lam_h, lam_a = tm.corner_lambdas
    assert lam_h == pytest.approx(8.0)
    assert lam_a == pytest.approx(4.0)
    assert tm.total_corners == 12
    assert "corners_total_9_5" in tm.baseline_probs


def test_dataset_feeds_walk_forward_end_to_end(session: Session) -> None:
    """Smoke test: the adapter output is consumable by run_walk_forward."""
    teams = [_team(session, name) for name in ("A", "B", "C", "D")]
    # Build a deterministic training corpus (older tournament).
    rng = np.random.default_rng(0)
    base = datetime(2018, 6, 1)
    n = 0
    for i in range(40):
        h_idx, a_idx = rng.choice(4, size=2, replace=False)
        _match(
            session,
            competition="INT-World Cup",
            season="2018",
            home=teams[h_idx],
            away=teams[a_idx],
            kickoff=base + timedelta(days=i),
            home_goals=int(rng.integers(0, 4)),
            away_goals=int(rng.integers(0, 4)),
        )
        n += 1
    # Held-out tournament fixtures.
    test_base = datetime(2022, 11, 21)
    for i in range(6):
        h_idx, a_idx = rng.choice(4, size=2, replace=False)
        _match(
            session,
            competition="INT-World Cup",
            season="2022",
            home=teams[h_idx],
            away=teams[a_idx],
            kickoff=test_base + timedelta(days=i),
            home_goals=int(rng.integers(0, 3)),
            away_goals=int(rng.integers(0, 3)),
        )

    training = load_training_matches(session, held_out=_TH)
    test_matches = load_test_matches(session, held_out=_TH)
    assert len(training) == n
    assert len(test_matches) == 6
    report = run_walk_forward(training_matches=training, test_matches=test_matches)
    # Every observable market in the held-out tournament was populated.
    for m in ("1x2", "ou_2_5", "btts"):
        assert report.pooled[m]["n"] == 6


def test_held_out_default_targets_odds_backed_tournaments() -> None:
    """Lock-in: held-out folds are the tournaments with an odds baseline
    (Euro 2016/2020/2024, WC 2018). Each trains on earlier tournaments via the
    walk-forward boundary. Changing this changes which tournaments train the
    model vs. score the gate, so the test exists to flag intentional edits."""
    assert HELD_OUT_TOURNAMENTS == (
        ("INT-European Championship", "2016"),
        ("INT-World Cup", "2018"),
        ("INT-European Championship", "2020"),
        ("INT-European Championship", "2024"),
    )

"""Tests for the Shin / multiplicative de-vig + DB baseline helper.

* **TEST-006** — Shin recovers true probabilities from a 4% overround book.
* Closed-form sanity for ``multiplicative``.
* Input validation (length, decimal-odds range).
* DB baseline helper averages fair probabilities across books, picks the
  newest snapshot per book, and surfaces ``None`` when no book provided a
  full outcome set.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session

from predictor.config import reset_settings_for_tests
from predictor.db.models import Match, OddsSnapshot, Team
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.odds.devig import (
    fair_probabilities_for_match,
    multiplicative,
    shin,
)

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed-form tests — TEST-006 + adjuncts
# ---------------------------------------------------------------------------


def _apply_overround(p_true: list[float], overround: float) -> list[float]:
    """Return decimal odds priced with a uniform multiplicative overround."""
    return [1.0 / (p * (1.0 + overround)) for p in p_true]


def test_shin_recovers_true_probabilities_under_uniform_overround() -> None:
    """TEST-006: 4% uniform overround → recovered probs within 0.5%."""
    p_true = [0.45, 0.28, 0.27]
    odds = _apply_overround(p_true, overround=0.04)

    fair = shin(odds)

    assert fair.sum() == pytest.approx(1.0, abs=1e-12)
    for recovered, truth in zip(fair, p_true, strict=True):
        assert abs(recovered - truth) <= 0.005


def test_multiplicative_recovers_uniform_overround_exactly() -> None:
    p_true = [0.5, 0.3, 0.2]
    odds = _apply_overround(p_true, overround=0.05)
    fair = multiplicative(odds)
    assert fair.sum() == pytest.approx(1.0, abs=1e-12)
    np.testing.assert_allclose(fair, p_true, atol=1e-12)


def test_shin_collapses_to_multiplicative_when_booksum_is_one() -> None:
    p_true = [0.6, 0.4]
    odds = [1.0 / p for p in p_true]  # zero overround
    fair = shin(odds)
    np.testing.assert_allclose(fair, p_true, atol=1e-12)


def test_devig_rejects_short_or_invalid_odds() -> None:
    with pytest.raises(ValueError, match="length"):
        shin([2.0])
    with pytest.raises(ValueError, match=r"> 1\.0"):
        multiplicative([2.0, 1.0])
    with pytest.raises(ValueError, match=r"> 1\.0"):
        shin([2.0, -3.0])


# ---------------------------------------------------------------------------
# DB baseline helper
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
def match_with_h2h_snapshots(db_url: str) -> Iterator[int]:
    """Two books, two fetched_at rounds, plus one partial-coverage book."""
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
        match_id: int = match.id

        old = datetime(2026, 6, 1, 12, 0)
        new = datetime(2026, 6, 2, 12, 0)

        # Old, stale snapshots — must be ignored.
        for outcome, odds in [("home", 2.2), ("draw", 3.6), ("away", 3.4)]:
            session.add(
                OddsSnapshot(
                    match_id=match_id,
                    book="pinnacle",
                    market="h2h",
                    outcome=outcome,
                    decimal_odds=odds,
                    fetched_at=old,
                )
            )

        # Newest pinnacle quote: 4% uniform overround on p=(0.45,0.28,0.27).
        for outcome, p_true in [("home", 0.45), ("draw", 0.28), ("away", 0.27)]:
            session.add(
                OddsSnapshot(
                    match_id=match_id,
                    book="pinnacle",
                    market="h2h",
                    outcome=outcome,
                    decimal_odds=1.0 / (p_true * 1.04),
                    fetched_at=new,
                )
            )

        # Newest paddypower quote: 6% uniform overround, same truth.
        for outcome, p_true in [("home", 0.45), ("draw", 0.28), ("away", 0.27)]:
            session.add(
                OddsSnapshot(
                    match_id=match_id,
                    book="paddypower",
                    market="h2h",
                    outcome=outcome,
                    decimal_odds=1.0 / (p_true * 1.06),
                    fetched_at=new,
                )
            )

        # Partial coverage — only "home" outcome; must be skipped from average.
        session.add(
            OddsSnapshot(
                match_id=match_id,
                book="grosvenor",
                market="h2h",
                outcome="home",
                decimal_odds=2.10,
                fetched_at=new,
            )
        )

        session.commit()
        yield match_id


def test_fair_probabilities_averages_across_books(match_with_h2h_snapshots: int) -> None:
    engine = get_engine()
    with Session(engine) as session:
        result = fair_probabilities_for_match(
            session, match_id=match_with_h2h_snapshots, market="h2h"
        )
    assert result is not None
    assert result.market == "h2h"
    assert set(result.books_used) == {"pinnacle", "paddypower"}
    assert sum(result.fair_by_outcome.values()) == pytest.approx(1.0, abs=1e-9)
    # Two books each near (0.45, 0.28, 0.27) → averaged baseline stays within 0.5%.
    assert abs(result.fair_by_outcome["home"] - 0.45) <= 0.005
    assert abs(result.fair_by_outcome["draw"] - 0.28) <= 0.005
    assert abs(result.fair_by_outcome["away"] - 0.27) <= 0.005


def test_fair_probabilities_multiplicative_method(match_with_h2h_snapshots: int) -> None:
    engine = get_engine()
    with Session(engine) as session:
        result = fair_probabilities_for_match(
            session,
            match_id=match_with_h2h_snapshots,
            market="h2h",
            method="multiplicative",
        )
    assert result is not None
    assert sum(result.fair_by_outcome.values()) == pytest.approx(1.0, abs=1e-9)
    # Multiplicative recovers uniform overround exactly per book — same truth.
    assert result.fair_by_outcome["home"] == pytest.approx(0.45, abs=1e-9)
    assert result.fair_by_outcome["draw"] == pytest.approx(0.28, abs=1e-9)
    assert result.fair_by_outcome["away"] == pytest.approx(0.27, abs=1e-9)


def test_fair_probabilities_returns_none_when_no_snapshots(db_url: str) -> None:
    engine = get_engine()
    with Session(engine) as session:
        result = fair_probabilities_for_match(session, match_id=999, market="h2h")
    assert result is None

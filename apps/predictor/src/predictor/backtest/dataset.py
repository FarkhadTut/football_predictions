"""Dataset adapter wiring the walk-forward backtest CLI to the DB (REQ-007).

Two public functions consumed by :func:`predictor.backtest.run._main`:

* :func:`load_training_matches` — pandas DataFrame of all completed matches
  whose ``(competition, season)`` pair is NOT in the held-out set. Columns
  match :data:`predictor.model.dixon_coles.REQUIRED_COLUMNS`.
* :func:`load_test_matches` — sequence of :class:`TestMatch` rows for the
  held-out tournament(s) with observed goals, de-vigged baseline market
  probabilities, and (when corner stats exist) Poisson corner rates.

Design points
-------------
* **Held-out set**: the tournaments with an odds baseline (Euro 2016/2020/2024,
  WC 2018) are each a walk-forward test fold. ``run._main`` passes the *full*
  match corpus as the training pool, so each fold trains on all tournaments
  before its kickoff (the temporal boundary in ``run_walk_forward`` prevents
  leakage). Override via the ``held_out`` argument (used by tests and re-tuning).
* **Baselines**: prefer the latest pre-kickoff ``OddsSnapshot`` per market,
  de-vigged with 1/odds-sum normalisation. When odds are missing we fall
  back to the training-set empirical base rates so the acceptance gate's
  "all four markets populated" precondition holds even on sparse data.
* **Corners**: per-team Poisson λ comes from the team's training-set
  corners-per-match average; ``total_corners`` is the sum of the two
  ``MatchStat.corners`` rows for the match. Markets where the stat is
  missing are skipped (handled by :func:`run._predict_one`).
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from sqlmodel import Session, select

from predictor.backtest.run import TestMatch
from predictor.db.models import Match, MatchStat, OddsSnapshot, Team
from predictor.db.session import get_engine

__all__ = [
    "CORNERS_LINE",
    "HELD_OUT_TOURNAMENTS",
    "load_test_matches",
    "load_training_matches",
]

# (competition, season) pairs held out as walk-forward test folds. Every
# tournament here that has an OddsPortal 1X2 baseline is scored against the
# market; each fold trains on all *earlier* tournaments (the walk-forward
# boundary in ``run_walk_forward`` enforces this — see ``_main``, which passes
# the full corpus as the training pool). WC 2014 is excluded as a fold because
# nothing precedes it to train on; WC 2022 is excluded because no results were
# ingested for it (FBref cache gap).
HELD_OUT_TOURNAMENTS: tuple[tuple[str, str], ...] = (
    ("INT-European Championship", "2016"),
    ("INT-World Cup", "2018"),
    ("INT-European Championship", "2020"),
    ("INT-European Championship", "2024"),
)

CORNERS_LINE: float = 9.5

# Map our market id → (OddsSnapshot.market, ordered outcome tuple).
# Outcomes are listed in the order :mod:`run` expects in the probability vector.
_MARKET_SPEC: dict[str, tuple[str, tuple[str, ...]]] = {
    "1x2": ("h2h", ("home", "draw", "away")),
    "ou_2_5": ("totals_2.5", ("over", "under")),
    "btts": ("btts", ("yes", "no")),
}

_TRAIN_COLS: tuple[str, ...] = (
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "kickoff_utc",
)


# ---------------------------------------------------------------------------
# Training corpus
# ---------------------------------------------------------------------------


def load_training_matches(
    session: Session | None = None,
    *,
    held_out: Iterable[tuple[str, str]] = HELD_OUT_TOURNAMENTS,
) -> pd.DataFrame:
    """Return finalised matches outside ``held_out`` as a DataFrame.

    The DataFrame is sorted by ``kickoff_utc`` ascending and contains the
    exact columns :meth:`DixonColesModel.fit` requires.
    """
    held: set[tuple[str, str]] = {(c, s) for c, s in held_out}
    own_session = session is None
    sess = session or Session(get_engine())
    try:
        rows = _final_match_rows(sess, exclude=held)
    finally:
        if own_session:
            sess.close()
    if not rows:
        return pd.DataFrame(columns=list(_TRAIN_COLS))
    df = pd.DataFrame.from_records(rows, columns=list(_TRAIN_COLS))
    df = df.sort_values("kickoff_utc", kind="stable").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Held-out test set
# ---------------------------------------------------------------------------


def load_test_matches(
    session: Session | None = None,
    *,
    held_out: Iterable[tuple[str, str]] = HELD_OUT_TOURNAMENTS,
) -> list[TestMatch]:
    """Build :class:`TestMatch` rows for every held-out tournament with results.

    Matches without ``home_goals`` / ``away_goals`` are skipped (no
    observation, no contribution to the Brier score).
    """
    held: list[tuple[str, str]] = [(c, s) for c, s in held_out]
    own_session = session is None
    sess = session or Session(get_engine())
    try:
        # Empirical base rates come from training data so the fallback
        # baseline is informative even when odds are absent.
        held_set: set[tuple[str, str]] = set(held)
        training_rates = _empirical_base_rates(sess, exclude=held_set)
        corner_means = _team_corner_means(sess, exclude=held_set)

        test_matches: list[TestMatch] = []
        for competition, season in held:
            test_matches.extend(
                _build_test_matches_for(
                    sess,
                    competition=competition,
                    season=season,
                    fallback=training_rates,
                    corner_means=corner_means,
                )
            )
    finally:
        if own_session:
            sess.close()
    return test_matches


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _final_matches(
    session: Session, *, exclude: set[tuple[str, str]] | None = None
) -> list[Match]:
    """All completed ``Match`` rows whose ``(competition, season)`` is not excluded."""
    stmt = (
        select(Match)
        .where(Match.home_goals.is_not(None))  # type: ignore[union-attr]
        .where(Match.away_goals.is_not(None))  # type: ignore[union-attr]
    )
    matches = list(session.exec(stmt).all())
    if exclude:
        matches = [m for m in matches if (m.competition, m.season) not in exclude]
    return matches


def _final_match_rows(
    session: Session, *, exclude: set[tuple[str, str]]
) -> list[dict[str, object]]:
    """Final matches projected to the training-corpus column dict."""
    rows: list[dict[str, object]] = []
    for m in _final_matches(session, exclude=exclude):
        assert m.home_goals is not None and m.away_goals is not None
        home = session.get(Team, m.home_team_id)
        away = session.get(Team, m.away_team_id)
        if home is None or away is None:
            continue
        rows.append(
            {
                "home_team": home.name,
                "away_team": away.name,
                "home_goals": int(m.home_goals),
                "away_goals": int(m.away_goals),
                "kickoff_utc": m.kickoff_utc,
            }
        )
    return rows


def _build_test_matches_for(
    session: Session,
    *,
    competition: str,
    season: str,
    fallback: dict[str, np.ndarray],
    corner_means: dict[str, float],
) -> list[TestMatch]:
    stmt = (
        select(Match)
        .where(Match.competition == competition)
        .where(Match.season == season)
        .where(Match.home_goals.is_not(None))  # type: ignore[union-attr]
        .where(Match.away_goals.is_not(None))  # type: ignore[union-attr]
        .order_by(Match.kickoff_utc)  # type: ignore[arg-type]
    )
    tournament_id = f"{competition}|{season}"
    test_matches: list[TestMatch] = []
    for m in session.exec(stmt).all():
        assert m.id is not None
        assert m.home_goals is not None and m.away_goals is not None
        home = session.get(Team, m.home_team_id)
        away = session.get(Team, m.away_team_id)
        if home is None or away is None:
            continue
        baseline = _baseline_probs(session, match_id=m.id, fallback=fallback)
        total_corners = _total_corners(session, match_id=m.id)
        lam_h = corner_means.get(home.name)
        lam_a = corner_means.get(away.name)
        corner_lambdas: tuple[float, float] | None
        if lam_h is not None and lam_a is not None and total_corners is not None:
            corner_lambdas = (float(lam_h), float(lam_a))
            baseline.setdefault(
                "corners_total_9_5",
                fallback.get(
                    "corners_total_9_5", np.array([0.5, 0.5], dtype=np.float64)
                ),
            )
        else:
            corner_lambdas = None
        test_matches.append(
            TestMatch(
                match_id=int(m.id),
                tournament_id=tournament_id,
                home_team=home.name,
                away_team=away.name,
                kickoff_utc=m.kickoff_utc,
                home_goals=int(m.home_goals),
                away_goals=int(m.away_goals),
                total_corners=total_corners,
                baseline_probs=baseline,
                corner_lambdas=corner_lambdas,
            )
        )
    return test_matches


def _baseline_probs(
    session: Session,
    *,
    match_id: int,
    fallback: dict[str, np.ndarray],
) -> dict[str, np.ndarray]:
    """Latest pre-kickoff ``OddsSnapshot`` per market, de-vigged.

    Returns the fallback empirical rates for any market without odds so the
    pooled report always has all observable markets populated.
    """
    out: dict[str, np.ndarray] = {}
    for market_id, (db_market, outcomes) in _MARKET_SPEC.items():
        stmt = (
            select(OddsSnapshot)
            .where(OddsSnapshot.match_id == match_id)
            .where(OddsSnapshot.market == db_market)
            .order_by(OddsSnapshot.fetched_at.desc())  # type: ignore[attr-defined]
        )
        # Take the latest decimal_odds per outcome.
        seen: dict[str, float] = {}
        for snap in session.exec(stmt).all():
            if snap.outcome not in seen and snap.outcome in outcomes:
                seen[snap.outcome] = float(snap.decimal_odds)
        if len(seen) == len(outcomes) and all(seen[o] > 1.0 for o in outcomes):
            implied = np.array([1.0 / seen[o] for o in outcomes], dtype=np.float64)
            out[market_id] = implied / implied.sum()
        else:
            out[market_id] = fallback[market_id].copy()
    return out


def _total_corners(session: Session, *, match_id: int) -> int | None:
    """Sum of per-team ``MatchStat.corners`` for the match, or None."""
    stmt = select(MatchStat).where(MatchStat.match_id == match_id)
    values = [int(s.corners) for s in session.exec(stmt).all() if s.corners is not None]
    if not values:
        return None
    return sum(values)


def _team_corner_means(
    session: Session, *, exclude: set[tuple[str, str]]
) -> dict[str, float]:
    """Per-team mean corners-per-match across training data."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    training = _final_matches(session, exclude=exclude)
    by_id = {m.id: m for m in training if m.id is not None}
    if not by_id:
        return {}
    stmt = select(MatchStat).where(MatchStat.match_id.in_(list(by_id)))  # type: ignore[attr-defined]
    for stat in session.exec(stmt).all():
        if stat.corners is None:
            continue
        team = session.get(Team, stat.team_id)
        if team is None:
            continue
        sums[team.name] = sums.get(team.name, 0.0) + float(stat.corners)
        counts[team.name] = counts.get(team.name, 0) + 1
    return {name: sums[name] / counts[name] for name in sums if counts[name] > 0}


def _empirical_base_rates(
    session: Session, *, exclude: set[tuple[str, str]]
) -> dict[str, np.ndarray]:
    """Frequencies of each outcome in the training data — used as a baseline
    fallback when no odds exist for a held-out match."""
    matches = _final_matches(session, exclude=exclude)
    if not matches:
        return {k: v.copy() for k, v in _UNIFORM_FALLBACK.items()}
    n = float(len(matches))
    h_wins = sum(
        1 for m in matches if (m.home_goals or 0) > (m.away_goals or 0)
    )
    a_wins = sum(
        1 for m in matches if (m.home_goals or 0) < (m.away_goals or 0)
    )
    draws = len(matches) - h_wins - a_wins
    over = sum(
        1 for m in matches if ((m.home_goals or 0) + (m.away_goals or 0)) > 2
    )
    btts_yes = sum(
        1
        for m in matches
        if (m.home_goals or 0) >= 1 and (m.away_goals or 0) >= 1
    )
    corner_over = _corner_over_rate(session, matches)
    return {
        "1x2": np.array([h_wins / n, draws / n, a_wins / n], dtype=np.float64),
        "ou_2_5": np.array([over / n, 1.0 - over / n], dtype=np.float64),
        "btts": np.array([btts_yes / n, 1.0 - btts_yes / n], dtype=np.float64),
        "corners_total_9_5": np.array(
            [corner_over, 1.0 - corner_over], dtype=np.float64
        ),
    }


def _corner_over_rate(session: Session, matches: list[Match]) -> float:
    """Fraction of training matches with total corners > CORNERS_LINE."""
    ids = [m.id for m in matches if m.id is not None]
    if not ids:
        return 0.5
    stmt = select(MatchStat).where(MatchStat.match_id.in_(ids))  # type: ignore[attr-defined]
    totals: dict[int, int] = {}
    for stat in session.exec(stmt).all():
        if stat.corners is None:
            continue
        totals[stat.match_id] = totals.get(stat.match_id, 0) + int(stat.corners)
    if not totals:
        return 0.5
    return sum(1 for t in totals.values() if t > CORNERS_LINE) / len(totals)


_UNIFORM_FALLBACK: dict[str, np.ndarray] = {
    "1x2": np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float64),
    "ou_2_5": np.array([0.5, 0.5], dtype=np.float64),
    "btts": np.array([0.5, 0.5], dtype=np.float64),
    "corners_total_9_5": np.array([0.5, 0.5], dtype=np.float64),
}

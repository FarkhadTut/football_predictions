"""Tests for the value-betting ROI simulation."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from predictor.backtest.roi import simulate_roi
from predictor.backtest.run import MatchPrediction


def _pred(match_id: int, probs: list[float], observed_idx: int) -> MatchPrediction:
    return MatchPrediction(
        match_id=match_id,
        tournament_id="T",
        kickoff_utc=datetime(2024, 1, 1),
        observed={"1x2": observed_idx},
        model_probs={"1x2": np.array(probs)},
        baseline_probs={"1x2": np.array(probs)},
    )


def test_simulate_roi_settles_value_bets() -> None:
    # Model loves home (p=0.6) at odds 2.0 -> EV 1.2 > 1, a value bet; home wins.
    # Draw/away EVs below 1 -> no bet.
    preds = [_pred(1, [0.6, 0.2, 0.2], observed_idx=0)]
    odds = {1: {"home": 2.0, "draw": 3.0, "away": 4.0}}
    r = simulate_roi(preds, odds, edge=0.0)
    assert r.n_bets == 1  # only home cleared EV>1
    assert r.staked == 1.0
    assert r.returned == 2.0  # home won at 2.0
    assert r.roi == 1.0
    assert r.win_rate == 1.0


def test_simulate_roi_losing_value_bet() -> None:
    # Same value bet on home, but away wins -> stake lost.
    preds = [_pred(1, [0.6, 0.2, 0.2], observed_idx=2)]
    odds = {1: {"home": 2.0, "draw": 3.0, "away": 4.0}}
    r = simulate_roi(preds, odds, edge=0.0)
    assert (r.n_bets, r.returned, r.roi) == (1, 0.0, -1.0)


def test_simulate_roi_edge_threshold_filters_bets() -> None:
    # EV(home) = 0.55 * 2.0 = 1.10. Bet at edge=0% and 5%, skip at 15%.
    preds = [_pred(1, [0.55, 0.25, 0.20], observed_idx=1)]
    odds = {1: {"home": 2.0, "draw": 1.5, "away": 2.0}}
    assert simulate_roi(preds, odds, edge=0.0).n_bets == 1
    assert simulate_roi(preds, odds, edge=0.05).n_bets == 1
    assert simulate_roi(preds, odds, edge=0.15).n_bets == 0


def test_simulate_roi_odds_bucket_filter() -> None:
    preds = [_pred(1, [0.34, 0.34, 0.34], observed_idx=0)]
    odds = {1: {"home": 2.0, "draw": 3.5, "away": 8.0}}
    # All three are value bets (EV = 0.34 * odds > 1 only for draw/away).
    # Restrict to longshots >=5 -> only the away bet (8.0) qualifies.
    r = simulate_roi(preds, odds, edge=0.0, min_odds=5.0)
    assert r.n_bets == 1  # away only
    assert r.returned == 0.0  # away didn't win (home did)


def test_simulate_roi_skips_matches_without_full_odds() -> None:
    preds = [_pred(1, [0.6, 0.2, 0.2], observed_idx=0)]
    odds = {1: {"home": 2.0, "draw": 3.0}}  # missing away
    r = simulate_roi(preds, odds, edge=0.0)
    assert (r.n_matches, r.n_bets) == (0, 0)

"""Value-betting ROI backtest of the tuned model vs OddsPortal 1X2 odds.

Runs the same walk-forward as the Brier gate (neutral venue, ridge=2,
half-life=540), then simulates level-stakes value betting at several edge
thresholds and reports yield/ROI. Answers "would this model have made money?",
which is the real-money question behind the project.

    uv run python scripts/roi_backtest.py
"""

from __future__ import annotations

import math
import sys

from predictor.backtest.dataset import load_test_matches, load_training_matches
from predictor.backtest.roi import load_h2h_odds, simulate_roi
from predictor.backtest.run import walk_forward_predictions
from predictor.db.session import get_session

_EDGES = (0.0, 0.02, 0.05, 0.10)
_BUCKETS = (("favourites <2.5", 0.0, 2.5), ("mid 2.5-5", 2.5, 5.0), ("longshots >=5", 5.0, math.inf))


def main() -> int:
    training = load_training_matches(held_out=())
    test_matches = load_test_matches()
    preds = walk_forward_predictions(
        training_matches=training,
        test_matches=test_matches,
        neutral_venue=True,
        ridge=2.0,
        half_life_days=540.0,
    )
    with get_session() as session:
        odds = load_h2h_odds(session, [p.match_id for p in preds])

    print("Value-betting ROI on 1X2 (OddsPortal consensus odds, level stakes)")
    print(f"{'edge':>5} | {'bets':>5} {'staked':>7} {'returned':>8} {'ROI':>8} {'win%':>6}")
    print("-" * 48)
    for edge in _EDGES:
        r = simulate_roi(preds, odds, edge=edge)
        print(
            f"{edge:>5.0%} | {r.n_bets:>5d} {r.staked:>7.1f} {r.returned:>8.1f} "
            f"{r.roi:>+7.1%} {r.win_rate:>6.1%}"
        )
    n_with_odds = sum(1 for p in preds if p.match_id in odds)
    print("-" * 48)
    print(f"matches: {len(preds)} predicted, {n_with_odds} with a 1X2 odds triple")

    # Where does the edge come from? Consensus-average odds most overstate real
    # payouts on longshots, so a longshot-concentrated ROI is a fragile signal.
    print("\nROI by odds bucket (edge 0%):")
    for label, lo, hi in _BUCKETS:
        r = simulate_roi(preds, odds, edge=0.0, min_odds=lo, max_odds=hi)
        print(f"  {label:>16}: {r.n_bets:>4d} bets  ROI {r.roi:>+7.1%}  win {r.win_rate:>5.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

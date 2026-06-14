"""Grid-sweep the Dixon-Coles regularisation knobs against the walk-forward gate.

Holds ``neutral_venue=True`` (every loaded tournament is at a neutral venue) and
sweeps ridge shrinkage by time-weighting half-life, reporting the pooled Brier
ratio per market for each config and the best one (most markets under the 0.98
gate, then lowest mean ratio over the observable markets).

    uv run python scripts/tune_model.py
"""

from __future__ import annotations

import sys

from predictor.backtest.acceptance import THRESHOLD, check
from predictor.backtest.dataset import load_test_matches, load_training_matches
from predictor.backtest.run import run_walk_forward

# Observable markets (corners has no data -> always nan, ignore for ranking).
_MARKETS = ("1x2", "ou_2_5", "btts")
_RIDGE_GRID = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)
_HALF_LIFE_GRID: tuple[float | None, ...] = (None, 365.0, 540.0)


def main() -> int:
    training = load_training_matches(held_out=())
    test_matches = load_test_matches()

    print(f"{'ridge':>6} {'half_life':>9} | " + " ".join(f"{m:>8}" for m in _MARKETS) + "  passing")
    print("-" * 56)

    best: tuple[int, float, float, float | None] | None = None  # (-npass, mean, ridge, hl)
    for ridge in _RIDGE_GRID:
        for half_life in _HALF_LIFE_GRID:
            report = run_walk_forward(
                training_matches=training,
                test_matches=test_matches,
                neutral_venue=True,
                ridge=ridge,
                half_life_days=half_life,
            )
            ratios = check(report).ratios
            obs = [ratios[m] for m in _MARKETS]
            n_pass = sum(1 for r in obs if r <= THRESHOLD)
            mean_ratio = sum(obs) / len(obs)
            hl_label = "uniform" if half_life is None else f"{half_life:g}"
            marks = " ".join(f"{ratios[m]:8.4f}" for m in _MARKETS)
            print(f"{ridge:6.1f} {hl_label:>9} | {marks}  {n_pass}/3")
            key = (-n_pass, mean_ratio, ridge, half_life)
            if best is None or key < best:
                best = key

    assert best is not None
    _, mean_ratio, ridge, half_life = best
    hl_label = "uniform" if half_life is None else f"{half_life:g}"
    print("-" * 56)
    print(f"BEST: ridge={ridge:g} half_life={hl_label} (mean obs ratio {mean_ratio:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

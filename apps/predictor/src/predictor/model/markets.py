"""Derive market marginals from a Dixon-Coles joint score matrix.

The model's primary output is a ``max_goals × max_goals`` joint
probability matrix ``M[x, y] = P(home_goals = x, away_goals = y)``
produced by ``DixonColesModel.predict``. This module converts that
matrix into the four Phase-0 betting markets:

* **1X2** — P(home win), P(draw), P(away win).
* **Over/Under 2.5 total goals** — split on ``x + y > 2``.
* **BTTS** — both teams to score: ``x ≥ 1 ∧ y ≥ 1``.

Because the joint matrix can omit tail mass above ``max_goals`` we
renormalize first; downstream marginals are then guaranteed to sum to
1 exactly (modulo floating-point error).

Corners are modelled separately via independent team Poisson rates; the
total-corners distribution is the convolution Poisson(λ_h + λ_a). The
public helper ``corner_total_prob_at_least`` returns ``P(total ≥ k)``
for thresholds used by the UI / acceptance tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from scipy.stats import poisson

__all__ = [
    "MarketMarginals",
    "corner_total_prob_at_least",
    "from_score_matrix",
]


@dataclass(frozen=True)
class MarketMarginals:
    """Marginal probabilities for the Phase-0 markets derived from a
    joint score matrix.

    All triples / pairs sum to 1.0 within floating-point error.
    """

    # 1X2
    p_home: float
    p_draw: float
    p_away: float
    # Over / under 2.5 total goals
    p_over_2_5: float
    p_under_2_5: float
    # Both teams to score
    p_btts_yes: float
    p_btts_no: float


def from_score_matrix(score_matrix: np.ndarray) -> MarketMarginals:
    """Derive 1X2 / O-U 2.5 / BTTS marginals from a joint score matrix.

    ``score_matrix[x, y]`` is treated as the joint probability of a
    home/away scoreline. The matrix is renormalized so that truncating
    the Poisson tail at ``max_goals`` does not leak into the marginals.
    """
    if score_matrix.ndim != 2 or score_matrix.shape[0] != score_matrix.shape[1]:
        raise ValueError(f"score_matrix must be a square 2-D array, got shape {score_matrix.shape}")
    total = float(score_matrix.sum())
    if total <= 0:
        raise ValueError("score_matrix has non-positive total mass")
    m = score_matrix / total
    n = m.shape[0]

    rows, cols = np.indices((n, n))
    # 1X2: triangular masks.
    p_home = float(m[rows > cols].sum())
    p_draw = float(np.trace(m))
    p_away = float(m[rows < cols].sum())

    # O/U 2.5: total goals = x + y, threshold strictly > 2.
    totals = rows + cols
    p_over_2_5 = float(m[totals > 2].sum())
    p_under_2_5 = float(m[totals <= 2].sum())

    # BTTS: both ≥ 1.
    p_btts_yes = float(m[(rows >= 1) & (cols >= 1)].sum())
    p_btts_no = float(m[(rows == 0) | (cols == 0)].sum())

    return MarketMarginals(
        p_home=p_home,
        p_draw=p_draw,
        p_away=p_away,
        p_over_2_5=p_over_2_5,
        p_under_2_5=p_under_2_5,
        p_btts_yes=p_btts_yes,
        p_btts_no=p_btts_no,
    )


def corner_total_prob_at_least(lambda_home: float, lambda_away: float, k: int) -> float:
    """``P(total corners ≥ k)`` under independent team Poisson rates.

    The sum of two independent Poissons is Poisson with rate
    ``λ_h + λ_a``, so the threshold tail follows directly from the
    survival function ``1 − F(k − 1)``.
    """
    if lambda_home < 0 or lambda_away < 0:
        raise ValueError("corner rates must be non-negative")
    if k < 0:
        raise ValueError("threshold k must be non-negative")
    return cast(float, poisson.sf(k - 1, lambda_home + lambda_away))

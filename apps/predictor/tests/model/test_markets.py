"""Tests for market-marginal derivation from a Dixon-Coles score matrix.

Covers TEST-004 (1X2 / O-U 2.5 / BTTS triples-and-pairs sum to 1 on any
valid 10×10 matrix) and TEST-005 (independent-team-Poisson corner totals
match the closed-form Poisson(λ_h + λ_a) survival function).
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import poisson

from predictor.model.dixon_coles import DixonColesModel
from predictor.model.markets import (
    MarketMarginals,
    corner_total_prob_at_least,
    from_score_matrix,
)

# ---------------------------------------------------------------------------
# TEST-004: market marginals from any valid 10×10 matrix sum to 1
# ---------------------------------------------------------------------------


def _assert_marginals_partition(m: MarketMarginals) -> None:
    assert m.p_home + m.p_draw + m.p_away == pytest.approx(1.0, abs=1e-9)
    assert m.p_over_2_5 + m.p_under_2_5 == pytest.approx(1.0, abs=1e-9)
    assert m.p_btts_yes + m.p_btts_no == pytest.approx(1.0, abs=1e-9)
    # No individual marginal may escape [0, 1].
    for v in (
        m.p_home,
        m.p_draw,
        m.p_away,
        m.p_over_2_5,
        m.p_under_2_5,
        m.p_btts_yes,
        m.p_btts_no,
    ):
        assert 0.0 <= v <= 1.0


def test_markets_partition_on_dixon_coles_matrix() -> None:
    """End-to-end: take a DC-shaped 10×10 matrix from the public helper
    and confirm derived marginals partition."""
    score_matrix = DixonColesModel._score_matrix(lam=1.4, mu=1.1, rho=-0.08, max_goals=10)
    marginals = from_score_matrix(score_matrix)
    _assert_marginals_partition(marginals)


def test_markets_partition_on_random_matrices() -> None:
    """Property check over 25 randomly generated 10×10 distributions."""
    rng = np.random.default_rng(7)
    for _ in range(25):
        raw = rng.random((10, 10))
        score_matrix = raw / raw.sum()
        _assert_marginals_partition(from_score_matrix(score_matrix))


def test_markets_hand_computed_directions() -> None:
    """Spot-check that the masks select the right cells, by hand."""
    m = np.zeros((4, 4))
    m[2, 1] = 0.30  # home win, total=3 → over, BTTS yes
    m[1, 1] = 0.20  # draw, total=2 → under, BTTS yes
    m[0, 0] = 0.15  # draw, total=0 → under, BTTS no
    m[1, 3] = 0.25  # away win, total=4 → over, BTTS yes
    m[0, 2] = 0.10  # away win, total=2 → under, BTTS no

    marginals = from_score_matrix(m)
    assert marginals.p_home == pytest.approx(0.30)
    assert marginals.p_draw == pytest.approx(0.35)  # (0,0) + (1,1)
    assert marginals.p_away == pytest.approx(0.35)  # (1,3) + (0,2)
    assert marginals.p_over_2_5 == pytest.approx(0.55)  # (2,1) + (1,3)
    assert marginals.p_under_2_5 == pytest.approx(0.45)
    assert marginals.p_btts_yes == pytest.approx(0.75)
    assert marginals.p_btts_no == pytest.approx(0.25)


def test_from_score_matrix_renormalizes_truncated_tail() -> None:
    """Truncating to max_goals drops tail mass; the helper must
    renormalize so triples still sum to 1."""
    # Tiny matrix with deliberate missing mass.
    m = np.array([[0.4, 0.1], [0.1, 0.2]])
    assert m.sum() < 1.0
    marginals = from_score_matrix(m)
    _assert_marginals_partition(marginals)


def test_from_score_matrix_rejects_non_square() -> None:
    with pytest.raises(ValueError, match="square"):
        from_score_matrix(np.zeros((3, 5)))


def test_from_score_matrix_rejects_empty_mass() -> None:
    with pytest.raises(ValueError, match="non-positive"):
        from_score_matrix(np.zeros((5, 5)))


# ---------------------------------------------------------------------------
# TEST-005: corner totals match Poisson(λ_h + λ_a) survival function
# ---------------------------------------------------------------------------


def test_corner_totals_match_closed_form_poisson() -> None:
    lam_h, lam_a = 5.2, 4.8
    lam_total = lam_h + lam_a
    for k in range(8, 13):
        expected = float(1.0 - poisson.cdf(k - 1, lam_total))
        actual = corner_total_prob_at_least(lam_h, lam_a, k)
        assert actual == pytest.approx(expected, abs=1e-12)


def test_corner_totals_at_zero_is_one() -> None:
    assert corner_total_prob_at_least(5.2, 4.8, 0) == pytest.approx(1.0, abs=1e-12)


def test_corner_totals_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        corner_total_prob_at_least(-1.0, 4.8, 5)
    with pytest.raises(ValueError, match="non-negative"):
        corner_total_prob_at_least(5.2, -0.1, 5)
    with pytest.raises(ValueError, match="non-negative"):
        corner_total_prob_at_least(5.2, 4.8, -1)

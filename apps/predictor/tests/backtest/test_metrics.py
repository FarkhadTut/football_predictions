"""Tests for backtest metrics — REQ-007, TEST-014.

* :func:`brier_score` — multi-outcome Brier on probability vectors vs one-hot outcomes.
* :func:`reliability` — uniform bin edges on [0, 1], per-bin empirical frequency,
  Expected Calibration Error matches a hand-computed reference within 1e-6.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from predictor.backtest.metrics import brier_score, reliability

# ---------------------------------------------------------------------------
# brier_score
# ---------------------------------------------------------------------------


def test_brier_score_perfect_prediction_is_zero() -> None:
    probs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    outcomes = np.array([0, 1])
    assert brier_score(probs, outcomes) == pytest.approx(0.0)


def test_brier_score_uniform_three_way_matches_closed_form() -> None:
    # Uniform 1/3 on a 3-way market: per-row Brier is
    # (1 - 1/3)^2 + 2 * (1/3)^2 = 4/9 + 2/9 = 6/9 = 2/3 (summing over outcomes).
    probs = np.full((4, 3), 1.0 / 3.0)
    outcomes = np.array([0, 1, 2, 0])
    assert brier_score(probs, outcomes) == pytest.approx(2.0 / 3.0)


def test_brier_score_binary_matches_squared_error() -> None:
    # Two-column probabilities encode a binary market; Brier equals mean squared
    # error vs the one-hot target (sum over both columns).
    probs = np.array([[0.8, 0.2], [0.3, 0.7], [0.6, 0.4]])
    outcomes = np.array([0, 1, 1])  # last one is wrong
    # Per-row: (0.2^2 + 0.2^2), (0.3^2 + 0.3^2), (0.6^2 + 0.6^2)
    expected = (0.08 + 0.18 + 0.72) / 3.0
    assert brier_score(probs, outcomes) == pytest.approx(expected)


def test_brier_score_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match=r"shape"):
        brier_score(np.zeros((3, 3)), np.zeros(2, dtype=int))


def test_brier_score_rejects_out_of_range_outcomes() -> None:
    with pytest.raises(ValueError, match=r"outcome"):
        brier_score(np.full((2, 3), 1 / 3), np.array([0, 5]))


# ---------------------------------------------------------------------------
# reliability (TEST-014)
# ---------------------------------------------------------------------------


def test_reliability_uniform_bin_edges() -> None:
    # 10 bins should produce 11 edges uniformly on [0, 1].
    probs = np.array([0.05, 0.55, 0.95])
    outcomes = np.array([0, 1, 1])
    result = reliability(probs, outcomes, n_bins=10)
    np.testing.assert_allclose(result.bin_edges, np.linspace(0.0, 1.0, 11))


def test_reliability_per_bin_frequency_matches_synthetic_profile() -> None:
    # Build a synthetic dataset where the empirical hit rate by bin is known.
    rng = np.random.default_rng(0)
    # 400 samples at p = 0.15: expect ~bin 1, hit rate ≈ 0.15
    p_low = np.full(400, 0.15)
    y_low = (rng.random(400) < 0.15).astype(int)
    # 400 samples at p = 0.85: expect ~bin 8, hit rate ≈ 0.85
    p_high = np.full(400, 0.85)
    y_high = (rng.random(400) < 0.85).astype(int)

    probs = np.concatenate([p_low, p_high])
    outcomes = np.concatenate([y_low, y_high])

    result = reliability(probs, outcomes, n_bins=10)
    # bin index 1 (p ∈ [0.1, 0.2)) holds the low cohort
    assert result.bin_counts[1] == 400
    assert result.bin_freq[1] == pytest.approx(y_low.mean(), abs=0.05)
    assert result.bin_pred[1] == pytest.approx(0.15)
    # bin index 8 (p ∈ [0.8, 0.9)) holds the high cohort
    assert result.bin_counts[8] == 400
    assert result.bin_freq[8] == pytest.approx(y_high.mean(), abs=0.05)
    assert result.bin_pred[8] == pytest.approx(0.85)


def test_reliability_ece_matches_hand_computed_reference() -> None:
    # Three samples in bin 0, three in bin 9; hand-compute ECE.
    probs = np.array([0.05, 0.05, 0.05, 0.95, 0.95, 0.95])
    outcomes = np.array([0, 0, 1, 1, 1, 0])
    # bin 0: pred=0.05, freq=1/3=0.333..., gap=0.283...
    # bin 9: pred=0.95, freq=2/3=0.666..., gap=0.283...
    # ECE = (3/6)*0.283... + (3/6)*0.283... = 0.283...
    expected_ece = abs(0.05 - 1.0 / 3.0)  # symmetric, both bins identical
    result = reliability(probs, outcomes, n_bins=10)
    assert result.ece == pytest.approx(expected_ece, abs=1e-9)


def test_reliability_empty_bins_contribute_zero() -> None:
    # All mass at p=0.5 (bin 5) — other bins empty and must not break ECE.
    probs = np.full(50, 0.5)
    outcomes = np.array([1] * 25 + [0] * 25)
    result = reliability(probs, outcomes, n_bins=10)
    assert result.bin_counts.sum() == 50
    # bin 5: pred=0.5, freq=0.5 → ECE = 0
    assert result.ece == pytest.approx(0.0, abs=1e-12)
    # empty bins report 0 freq / pred but contribute zero weight
    assert math.isfinite(result.ece)

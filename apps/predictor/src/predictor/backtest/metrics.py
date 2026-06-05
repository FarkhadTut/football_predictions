"""Scoring + calibration metrics for the walk-forward backtest.

REQ-007 demands a Brier-score gate; TEST-014 demands reliability diagrams with
hand-verifiable per-bin frequencies and Expected Calibration Error.

Conventions
-----------
* ``probs`` is a ``(n_samples, n_outcomes)`` array of probabilities (rows
  should sum to 1.0 up to floating-point slop). ``outcomes`` is an integer
  array of observed outcome indices ``∈ [0, n_outcomes)``.
* :func:`brier_score` returns the *multi-outcome* Brier score, i.e. the mean
  over rows of ``Σ_k (p_k - 1[y == k])²`` — the natural extension of the
  binary Brier score, and the one consumed by the acceptance gate.
* :func:`reliability` operates on a single column of probabilities + binary
  outcomes; callers slice their multi-outcome predictions per market.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np


def brier_score(probs: np.ndarray, outcomes: np.ndarray) -> float:
    """Multi-outcome Brier score.

    For each row, computes ``Σ_k (p_k - 1[y == k])²`` and returns the mean.
    Reduces to the standard squared-error Brier for binary markets when
    ``probs`` has 2 columns.
    """
    probs = np.asarray(probs, dtype=np.float64)
    outcomes = np.asarray(outcomes, dtype=np.int64)
    if probs.ndim != 2 or outcomes.ndim != 1 or probs.shape[0] != outcomes.shape[0]:
        raise ValueError(f"shape mismatch: probs {probs.shape} vs outcomes {outcomes.shape}")
    n_outcomes = probs.shape[1]
    if outcomes.min(initial=0) < 0 or outcomes.max(initial=0) >= n_outcomes:
        raise ValueError(
            f"outcome indices must lie in [0, {n_outcomes}); got "
            f"min={outcomes.min(initial=0)}, max={outcomes.max(initial=0)}"
        )

    one_hot = np.zeros_like(probs)
    one_hot[np.arange(len(outcomes)), outcomes] = 1.0
    sq_err = np.sum((probs - one_hot) ** 2, axis=1)
    return float(np.mean(sq_err))


@dataclass(frozen=True)
class ReliabilityResult:
    """Reliability-diagram payload (TEST-014).

    Attributes
    ----------
    bin_edges
        ``n_bins + 1`` uniform edges on ``[0, 1]``.
    bin_counts
        Sample count per bin.
    bin_pred
        Mean predicted probability per bin (0 in empty bins).
    bin_freq
        Empirical positive rate per bin (0 in empty bins).
    ece
        Expected Calibration Error = ``Σ_b (n_b / N) · |pred_b - freq_b|``.
        Empty bins contribute zero.
    """

    bin_edges: np.ndarray
    bin_counts: np.ndarray
    bin_pred: np.ndarray
    bin_freq: np.ndarray
    ece: float


def reliability(probs: np.ndarray, outcomes: np.ndarray, *, n_bins: int = 10) -> ReliabilityResult:
    """Reliability bins + ECE for a single binary market.

    ``probs`` is a 1-D array of predicted P(outcome=1); ``outcomes`` is the
    matching 0/1 array. Probabilities are bucketed into ``n_bins`` equal-width
    bins on ``[0, 1]``; the right edge of the last bin is inclusive so
    ``p = 1.0`` lands in the final bin.
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be ≥ 1, got {n_bins}")
    probs = np.asarray(probs, dtype=np.float64).ravel()
    outcomes = np.asarray(outcomes, dtype=np.int64).ravel()
    if probs.shape != outcomes.shape:
        raise ValueError(f"shape mismatch: probs {probs.shape} vs outcomes {outcomes.shape}")

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # np.digitize with right=False returns 1..n_bins for in-range values, so
    # subtract 1; clip p == 1.0 (would land in n_bins) back into the last bin.
    bin_ix = np.clip(np.digitize(probs, edges[1:-1], right=False), 0, n_bins - 1)

    counts = np.zeros(n_bins, dtype=np.int64)
    pred_sum = np.zeros(n_bins, dtype=np.float64)
    freq_sum = np.zeros(n_bins, dtype=np.float64)
    np.add.at(counts, bin_ix, 1)
    np.add.at(pred_sum, bin_ix, probs)
    np.add.at(freq_sum, bin_ix, outcomes.astype(np.float64))

    safe_counts = np.where(counts > 0, counts, 1)
    bin_pred = cast(np.ndarray, pred_sum / safe_counts)
    bin_freq = cast(np.ndarray, freq_sum / safe_counts)
    bin_pred = np.where(counts > 0, bin_pred, 0.0)
    bin_freq = np.where(counts > 0, bin_freq, 0.0)

    total = int(counts.sum())
    ece = 0.0 if total == 0 else float(np.sum(counts / total * np.abs(bin_pred - bin_freq)))

    return ReliabilityResult(
        bin_edges=edges,
        bin_counts=counts,
        bin_pred=bin_pred,
        bin_freq=bin_freq,
        ece=ece,
    )

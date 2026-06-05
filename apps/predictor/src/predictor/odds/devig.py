"""Convert bookmaker decimal odds into fair (overround-free) probabilities.

Two methods:

* :func:`multiplicative` — divide each implied probability ``1/o`` by the
  booksum ``B = Σ 1/o``. Optimal when the bookmaker's overround is applied
  uniformly across outcomes. Cheap, has a closed form.

* :func:`shin` — Shin (1992) model that attributes the overround to a
  proportion ``z`` of trades against informed bettors. Given a booksum
  ``B`` and per-outcome implied probabilities ``b_i = 1/o_i``, the fair
  probability is

  ``π_i = (√(z² + 4(1-z)·b_i²/B) - z) / (2(1-z))``

  with ``z ∈ [0, 1)`` chosen so the ``π_i`` sum to 1. We solve the 1-D
  root with ``scipy.optimize.brentq`` because the LHS minus 1 is monotone
  in ``z`` over the feasible interval. For uniform overround the solver
  converges to ``z ≈ 0`` and Shin collapses to multiplicative — TEST-006
  exercises this case.

The :func:`fair_probabilities_for_match` helper consumes the latest
``odds_snapshots`` rows for a given ``(match_id, market)`` and returns one
averaged fair distribution across the available books — the implied-odds
baseline the backtest compares the Dixon-Coles output against.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
from scipy.optimize import brentq
from sqlmodel import Session, col, select

from predictor.db.models import OddsSnapshot

__all__ = [
    "DevigMethod",
    "FairProbabilities",
    "fair_probabilities_for_match",
    "multiplicative",
    "shin",
]

DevigMethod = Literal["shin", "multiplicative"]


# ---------------------------------------------------------------------------
# Closed-form de-vig
# ---------------------------------------------------------------------------


def _implied(book_odds: Sequence[float]) -> np.ndarray:
    arr = np.asarray(book_odds, dtype=float)
    if arr.ndim != 1 or arr.size < 2:
        raise ValueError("book_odds must be a 1-D sequence of length ≥ 2")
    if np.any(arr <= 1.0):
        raise ValueError("decimal odds must be > 1.0")
    return 1.0 / arr


def multiplicative(book_odds: Sequence[float]) -> np.ndarray:
    """Divide each implied probability by the booksum.

    Fast and exact when overround is applied uniformly across outcomes.
    Always returns a vector summing to 1.
    """
    b = _implied(book_odds)
    return cast(np.ndarray, b / b.sum())


# ---------------------------------------------------------------------------
# Shin (1992)
# ---------------------------------------------------------------------------


def _shin_pi(z: float, b: np.ndarray, booksum: float) -> np.ndarray:
    inner = z * z + 4.0 * (1.0 - z) * (b * b) / booksum
    return cast(np.ndarray, (np.sqrt(inner) - z) / (2.0 * (1.0 - z)))


def shin(book_odds: Sequence[float], *, tol: float = 1e-12) -> np.ndarray:
    """Solve the Shin (1992) model for fair probabilities.

    Returns a vector of fair probabilities summing to 1.0. If the booksum
    is ≤ 1 (no overround), the result equals the multiplicative output
    with ``z = 0``.
    """
    b = _implied(book_odds)
    booksum = float(b.sum())
    if booksum <= 1.0 + tol:
        # No overround to remove — Shin would push z to 0 anyway.
        return b / booksum

    def residual(z: float) -> float:
        return float(_shin_pi(z, b, booksum).sum() - 1.0)

    # residual(0) = booksum - 1 > 0 ; residual(z→1⁻) → -∞. Bracket inside (0, 1).
    z_hi = 0.999999
    if residual(z_hi) > 0:
        # Extreme overround — return multiplicative as the most conservative fallback.
        return b / booksum
    z = brentq(residual, 0.0, z_hi, xtol=tol)
    pi = _shin_pi(z, b, booksum)
    # Tiny numerical re-normalisation to guarantee an exact partition.
    return cast(np.ndarray, pi / pi.sum())


# ---------------------------------------------------------------------------
# DB helper: implied baseline per match per market
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FairProbabilities:
    """Averaged fair probabilities for a ``(match, market)`` pair."""

    market: str
    fair_by_outcome: dict[str, float]
    books_used: tuple[str, ...]


def _latest_snapshot_per_book(
    session: Session, *, match_id: int, market: str
) -> dict[str, list[OddsSnapshot]]:
    """Group the most-recent snapshot per book by ``fetched_at``."""
    rows = session.exec(
        select(OddsSnapshot)
        .where(OddsSnapshot.match_id == match_id, OddsSnapshot.market == market)
        .order_by(col(OddsSnapshot.fetched_at).desc())
    ).all()
    latest_at: dict[str, object] = {}
    by_book: dict[str, list[OddsSnapshot]] = defaultdict(list)
    for row in rows:
        seen_at = latest_at.setdefault(row.book, row.fetched_at)
        if row.fetched_at == seen_at:
            by_book[row.book].append(row)
    return by_book


def fair_probabilities_for_match(
    session: Session,
    *,
    match_id: int,
    market: str,
    method: DevigMethod = "shin",
) -> FairProbabilities | None:
    """Implied-odds baseline for one ``(match, market)``.

    Pulls each book's latest snapshot, de-vigs that book's outcome
    triple/pair, then averages across books that quoted the full set.
    Returns ``None`` if no book has a complete quote.
    """
    by_book = _latest_snapshot_per_book(session, match_id=match_id, market=market)
    if not by_book:
        return None

    per_book: list[tuple[str, dict[str, float]]] = []
    expected_outcomes: set[str] | None = None
    for book, rows in by_book.items():
        outcomes = sorted({r.outcome for r in rows})
        if expected_outcomes is None:
            expected_outcomes = set(outcomes)
        if set(outcomes) != expected_outcomes:
            # Skip books with partial coverage — keeps the average comparable.
            continue
        # Stable ordering so the de-vig vector lines up with `outcomes`.
        rows_by_outcome = {r.outcome: r for r in rows}
        ordered_odds = [rows_by_outcome[o].decimal_odds for o in outcomes]
        fair = shin(ordered_odds) if method == "shin" else multiplicative(ordered_odds)
        per_book.append((book, dict(zip(outcomes, fair.tolist(), strict=True))))

    if not per_book or expected_outcomes is None:
        return None

    averaged: dict[str, float] = dict.fromkeys(sorted(expected_outcomes), 0.0)
    for _, fair_map in per_book:
        for outcome, value in fair_map.items():
            averaged[outcome] += value
    n = float(len(per_book))
    for outcome in averaged:
        averaged[outcome] /= n
    return FairProbabilities(
        market=market,
        fair_by_outcome=averaged,
        books_used=tuple(book for book, _ in per_book),
    )

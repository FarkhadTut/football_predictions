"""Apply Claude qualitative notes to model marginals (the Phase 1 layer).

The Dixon-Coles model is a goal model — it knows nothing about injuries, team
news, motivation, or tactics. A :class:`ClaudeNote` carries per-market
``log_odds_shift`` values (its ``qualitative_deltas``); this module folds those
shifts into the model's :class:`MarketMarginals` to produce a blended
prediction.

Sign convention (from the ``ClaudeNote`` schema): a **positive** ``log_odds_shift``
moves the market toward ``home`` / ``over`` / ``yes``.

The blend is leakage-bound to *forward* use only — Claude knows historical
results, so notes authored about past matches are not trustworthy and this layer
must not be "backtested" on history.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Sequence

from predictor_schemas import ClaudeNote, QualitativeDelta

from predictor.model.markets import MarketMarginals

__all__ = ["apply_note", "apply_qualitative_deltas", "logit", "sigmoid"]

logger = logging.getLogger(__name__)

_EPS = 1e-9


def logit(p: float) -> float:
    """Log-odds of ``p``, clamped to ``(EPS, 1-EPS)`` so it stays finite."""
    clamped = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(clamped / (1.0 - clamped))


def sigmoid(x: float) -> float:
    """Inverse of :func:`logit`."""
    return 1.0 / (1.0 + math.exp(-x))


def _shift_binary(p_yes: float, delta: float) -> tuple[float, float]:
    """Shift a 2-outcome market by ``delta`` in log-odds; return ``(yes, no)``."""
    yes = sigmoid(logit(p_yes) + delta)
    return yes, 1.0 - yes


def apply_qualitative_deltas(
    marginals: MarketMarginals,
    deltas: Sequence[QualitativeDelta],
) -> MarketMarginals:
    """Return ``marginals`` with each market's ``log_odds_shift`` applied.

    Shifts are summed per market first, so the result is independent of delta
    order. Each market is renormalised independently and stays a valid
    distribution; ``delta == 0`` is the identity. ``corners_total`` deltas are
    skipped (corners are not part of ``MarketMarginals`` — they come from a
    separate Poisson and have no odds in Phase 0).
    """
    by_market: dict[str, float] = defaultdict(float)
    for d in deltas:
        by_market[d.market] += d.log_odds_shift

    p_home, p_draw, p_away = marginals.p_home, marginals.p_draw, marginals.p_away
    if "1x2" in by_market:
        # Symmetric shift on the home–away axis (draw fixed in log-space), so the
        # home/away log-odds move by exactly the delta. Positive → toward home.
        delta = by_market["1x2"]
        h = p_home * math.exp(delta / 2.0)
        a = p_away * math.exp(-delta / 2.0)
        total = h + a + p_draw
        p_home, p_draw, p_away = h / total, p_draw / total, a / total

    p_over, p_under = marginals.p_over_2_5, marginals.p_under_2_5
    if "ou_2_5" in by_market:
        p_over, p_under = _shift_binary(p_over, by_market["ou_2_5"])

    p_yes, p_no = marginals.p_btts_yes, marginals.p_btts_no
    if "btts" in by_market:
        p_yes, p_no = _shift_binary(p_yes, by_market["btts"])

    if "corners_total" in by_market:
        logger.info(
            "corners_total delta (%+.3f) ignored: corners are not part of MarketMarginals",
            by_market["corners_total"],
        )

    return MarketMarginals(
        p_home=p_home,
        p_draw=p_draw,
        p_away=p_away,
        p_over_2_5=p_over,
        p_under_2_5=p_under,
        p_btts_yes=p_yes,
        p_btts_no=p_no,
    )


def apply_note(marginals: MarketMarginals, note: ClaudeNote) -> MarketMarginals:
    """Convenience: apply a whole :class:`ClaudeNote`'s deltas to ``marginals``."""
    return apply_qualitative_deltas(marginals, note.qualitative_deltas)

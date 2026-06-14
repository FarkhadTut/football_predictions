"""Value-betting ROI simulation over the walk-forward held-out matches.

Brier (see :mod:`predictor.backtest.run`) measures *calibration* — whether the
model's probabilities match reality. ROI measures *profitability* — whether
betting the model's disagreements with the book would have made money. A model
can be slightly worse-calibrated than the market overall yet still profitable if
it is selective, staking only where its edge is positive.

Rule (level stakes): for each held-out match, bet one unit on every 1X2 outcome
whose **expected value** ``model_prob * decimal_odds`` exceeds ``1 + edge``.
Settle at the *raw* OddsPortal decimal odds (the price actually offered, vig
included — not the de-vigged baseline). Only ``1x2`` is simulated: it is the
only market with real book odds in ``odds_snapshots`` (O/U + BTTS were deferred).

A positive ROI here is a strong signal because OddsPortal odds are a
consensus-*average* line (sharper than any single soft book); a real bettor at a
softer book would do at least as well.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sqlmodel import Session, select

from predictor.backtest.run import MatchPrediction
from predictor.db.models import OddsSnapshot

__all__ = ["RoiResult", "load_h2h_odds", "simulate_roi"]

_OUTCOMES: tuple[str, ...] = ("home", "draw", "away")  # indices 0,1,2 in model_probs["1x2"]
_BOOK = "oddsportal"


@dataclass(frozen=True)
class RoiResult:
    edge: float
    n_matches: int  # held-out matches that had a complete odds triple
    n_bets: int
    staked: float
    returned: float
    win_rate: float

    @property
    def profit(self) -> float:
        return self.returned - self.staked

    @property
    def roi(self) -> float:
        return self.profit / self.staked if self.staked > 0 else 0.0


def load_h2h_odds(session: Session, match_ids: Sequence[int]) -> dict[int, dict[str, float]]:
    """Raw OddsPortal 1X2 decimal odds per match: ``{match_id: {outcome: odds}}``.

    One bulk query (no N+1). Keeps the latest ``fetched_at`` per outcome.
    """
    if not match_ids:
        return {}
    rows = session.exec(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.match_id.in_(set(match_ids)),  # type: ignore[attr-defined]
            OddsSnapshot.book == _BOOK,
            OddsSnapshot.market == "h2h",
        )
        .order_by(OddsSnapshot.fetched_at)  # type: ignore[arg-type]
    ).all()
    out: dict[int, dict[str, float]] = {}
    for r in rows:  # ascending fetched_at → last write wins (latest)
        out.setdefault(r.match_id, {})[r.outcome] = float(r.decimal_odds)
    return out


def simulate_roi(
    predictions: Sequence[MatchPrediction],
    odds_by_match: dict[int, dict[str, float]],
    *,
    edge: float = 0.0,
    stake: float = 1.0,
    min_odds: float = 0.0,
    max_odds: float = math.inf,
) -> RoiResult:
    """Level-stakes value-betting ROI on 1X2 at threshold ``edge``.

    ``min_odds``/``max_odds`` restrict betting to an odds bucket — useful to
    show that apparent edge is (or isn't) concentrated in high-variance
    longshots, where consensus-average odds most overstate real payouts.
    """
    n_matches = n_bets = wins = 0
    staked = returned = 0.0
    for pred in predictions:
        probs = pred.model_probs.get("1x2")
        if probs is None:
            continue
        raw = odds_by_match.get(pred.match_id)
        if raw is None or not all(o in raw for o in _OUTCOMES):
            continue
        n_matches += 1
        observed = pred.observed["1x2"]
        for i, outcome in enumerate(_OUTCOMES):
            odds = raw[outcome]
            if not (min_odds <= odds < max_odds):
                continue
            if float(probs[i]) * odds > 1.0 + edge:
                n_bets += 1
                staked += stake
                if observed == i:
                    returned += stake * odds
                    wins += 1
    return RoiResult(
        edge=edge,
        n_matches=n_matches,
        n_bets=n_bets,
        staked=staked,
        returned=returned,
        win_rate=(wins / n_bets if n_bets else 0.0),
    )

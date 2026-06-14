"""Tests for the qualitative blending layer (apply notes to model marginals)."""

from __future__ import annotations

import math

import pytest
from predictor_schemas.claude_note import (
    Delta1x2,
    DeltaBTTS,
    DeltaCornersTotal,
    DeltaOU25,
)

from predictor.model.markets import MarketMarginals
from predictor.model.qualitative import (
    apply_qualitative_deltas,
    logit,
    sigmoid,
)


def _marginals(
    *, home: float = 0.5, draw: float = 0.3, away: float = 0.2, over: float = 0.55, yes: float = 0.6
) -> MarketMarginals:
    return MarketMarginals(
        p_home=home,
        p_draw=draw,
        p_away=away,
        p_over_2_5=over,
        p_under_2_5=1.0 - over,
        p_btts_yes=yes,
        p_btts_no=1.0 - yes,
    )


def test_logit_sigmoid_roundtrip() -> None:
    for p in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert sigmoid(logit(p)) == pytest.approx(p)
    # Clamping keeps the extremes finite.
    assert math.isfinite(logit(0.0))
    assert math.isfinite(logit(1.0))


def test_1x2_positive_shift_moves_toward_home() -> None:
    m = _marginals(home=0.5, draw=0.3, away=0.2)
    out = apply_qualitative_deltas(m, [Delta1x2(log_odds_shift=0.5)])
    assert out.p_home > m.p_home
    assert out.p_away < m.p_away
    assert out.p_home + out.p_draw + out.p_away == pytest.approx(1.0)
    # Home/away log-odds move by exactly the delta.
    moved = math.log(out.p_home / out.p_away) - math.log(m.p_home / m.p_away)
    assert moved == pytest.approx(0.5)


def test_1x2_negative_shift_moves_toward_away() -> None:
    m = _marginals()
    out = apply_qualitative_deltas(m, [Delta1x2(log_odds_shift=-0.7)])
    assert out.p_away > m.p_away
    assert out.p_home < m.p_home


def test_ou_positive_shift_moves_toward_over() -> None:
    m = _marginals(over=0.55)
    out = apply_qualitative_deltas(m, [DeltaOU25(log_odds_shift=0.4)])
    assert out.p_over_2_5 > m.p_over_2_5
    assert out.p_over_2_5 + out.p_under_2_5 == pytest.approx(1.0)
    assert logit(out.p_over_2_5) - logit(m.p_over_2_5) == pytest.approx(0.4)


def test_btts_positive_shift_moves_toward_yes() -> None:
    m = _marginals(yes=0.6)
    out = apply_qualitative_deltas(m, [DeltaBTTS(log_odds_shift=0.3)])
    assert out.p_btts_yes > m.p_btts_yes
    assert out.p_btts_yes + out.p_btts_no == pytest.approx(1.0)
    assert logit(out.p_btts_yes) - logit(m.p_btts_yes) == pytest.approx(0.3)


def test_zero_delta_is_identity() -> None:
    m = _marginals()
    out = apply_qualitative_deltas(m, [Delta1x2(log_odds_shift=0.0), DeltaOU25(log_odds_shift=0.0)])
    assert out == m


def test_deltas_compound_per_market() -> None:
    m = _marginals()
    once = apply_qualitative_deltas(m, [Delta1x2(log_odds_shift=0.6)])
    twice = apply_qualitative_deltas(
        m, [Delta1x2(log_odds_shift=0.4), Delta1x2(log_odds_shift=0.2)]
    )
    assert twice.p_home == pytest.approx(once.p_home)
    assert twice.p_away == pytest.approx(once.p_away)


def test_corners_delta_is_noop_on_marginals() -> None:
    m = _marginals()
    out = apply_qualitative_deltas(m, [DeltaCornersTotal(log_odds_shift=0.9)])
    assert out == m


def test_empty_deltas_is_identity() -> None:
    m = _marginals()
    assert apply_qualitative_deltas(m, []) == m

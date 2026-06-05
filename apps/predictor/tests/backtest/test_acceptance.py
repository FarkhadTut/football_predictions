"""Tests for the backtest acceptance gate — REQ-007, TEST-007.

The gate fails CI iff fewer than 3 of {1x2, ou_2_5, btts, corners_total_9_5}
markets satisfy ``model_brier / baseline_brier ≤ 0.98`` on the pooled report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from predictor.backtest.acceptance import (
    MARKETS,
    MIN_MARKETS_PASSING,
    THRESHOLD,
    BacktestReport,
    check,
    load_report,
)


def _report(pairs: dict[str, tuple[float, float]]) -> BacktestReport:
    pooled = {m: {"model_brier": mb, "baseline_brier": bb} for m, (mb, bb) in pairs.items()}
    return BacktestReport(pooled=pooled, per_tournament={})


def test_check_passes_when_three_of_four_markets_beat_threshold() -> None:
    # 1X2 fails (ratio 0.99), other three pass at exactly 0.98.
    r = _report(
        {
            "1x2": (0.198, 0.200),  # 0.99 — over threshold
            "ou_2_5": (0.196, 0.200),  # 0.98 — exactly at threshold (pass)
            "btts": (0.196, 0.200),
            "corners_total_9_5": (0.196, 0.200),
        }
    )
    result = check(r)
    assert result.passed is True
    assert result.passing_markets == {"ou_2_5", "btts", "corners_total_9_5"}
    assert result.failing_markets == {"1x2"}
    assert result.ratios["1x2"] == pytest.approx(0.99)
    assert result.ratios["ou_2_5"] == pytest.approx(0.98)


def test_check_fails_when_two_markets_miss() -> None:
    r = _report(
        {
            "1x2": (0.198, 0.200),  # 0.99 fail
            "ou_2_5": (0.198, 0.200),  # 0.99 fail
            "btts": (0.196, 0.200),  # 0.98 pass
            "corners_total_9_5": (0.196, 0.200),  # 0.98 pass
        }
    )
    result = check(r)
    assert result.passed is False
    assert len(result.passing_markets) == 2
    assert {"1x2", "ou_2_5"} <= result.failing_markets


def test_check_requires_all_four_markets_in_pooled() -> None:
    r = _report({m: (0.18, 0.20) for m in MARKETS if m != "corners_total_9_5"})
    with pytest.raises(ValueError, match=r"corners_total_9_5"):
        check(r)


def test_check_constants_match_spec() -> None:
    # Lock in REQ-007 numerics so a silent change to the gate breaks loudly.
    assert THRESHOLD == 0.98
    assert MIN_MARKETS_PASSING == 3
    assert MARKETS == ("1x2", "ou_2_5", "btts", "corners_total_9_5")


def test_load_report_parses_sidecar_json(tmp_path: Path) -> None:
    payload = {
        "pooled": {m: {"model_brier": 0.196, "baseline_brier": 0.200} for m in MARKETS},
        "per_tournament": {
            "EURO_2024": {
                m: {"model_brier": 0.19, "baseline_brier": 0.20, "n": 51} for m in MARKETS
            }
        },
    }
    p = tmp_path / "report.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    report = load_report(p)
    assert isinstance(report, BacktestReport)
    assert set(report.pooled) == set(MARKETS)
    result = check(report)
    assert result.passed is True


def test_acceptance_result_as_pytest_assertion() -> None:
    # The intended usage from TEST-007: a single assert against the result.
    r = _report({m: (0.196, 0.200) for m in MARKETS})
    result = check(r)
    assert result.passed, result.summary()

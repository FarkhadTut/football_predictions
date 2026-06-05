"""Tests for the walk-forward backtest harness — REQ-007.

* :func:`aggregate` — bucketing by tournament + market, missing-outcome skip
  semantics, pooled vs per-tournament correctness.
* :func:`run_walk_forward` — DC refits on a synthetic dataset where the
  model has a structural edge over a noisy baseline; pooled report is
  produced with the expected market keys.
* :func:`render_markdown` / :func:`write_reports` — markdown contains the
  acceptance verdict + pooled table; JSON sidecar round-trips through
  :func:`acceptance.load_report`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from predictor.backtest.acceptance import MARKETS, check, load_report
from predictor.backtest.run import (
    MatchPrediction,
    TestMatch,
    aggregate,
    render_markdown,
    run_walk_forward,
    to_json_payload,
    write_reports,
)

# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def _pred(
    match_id: int,
    *,
    tournament: str = "T1",
    home_goals: int = 1,
    away_goals: int = 0,
    model_home: float = 0.6,
    base_home: float = 0.4,
) -> MatchPrediction:
    return MatchPrediction(
        match_id=match_id,
        tournament_id=tournament,
        kickoff_utc=datetime(2024, 6, 1) + timedelta(days=match_id),
        observed={"1x2": 0, "ou_2_5": 1, "btts": 1},
        model_probs={
            "1x2": np.array([model_home, (1 - model_home) / 2, (1 - model_home) / 2]),
            "ou_2_5": np.array([0.4, 0.6]),
            "btts": np.array([0.5, 0.5]),
        },
        baseline_probs={
            "1x2": np.array([base_home, (1 - base_home) / 2, (1 - base_home) / 2]),
            "ou_2_5": np.array([0.5, 0.5]),
            "btts": np.array([0.5, 0.5]),
        },
    )


def test_aggregate_buckets_by_tournament_and_pools_separately() -> None:
    rows = [
        _pred(1, tournament="A", model_home=0.7),
        _pred(2, tournament="A", model_home=0.6),
        _pred(3, tournament="B", model_home=0.55),
    ]
    report = aggregate(rows)
    assert set(report.per_tournament) == {"A", "B"}
    assert report.per_tournament["A"]["1x2"]["n"] == 2
    assert report.per_tournament["B"]["1x2"]["n"] == 1
    assert report.pooled["1x2"]["n"] == 3
    # Pooled brier ≈ weighted mean of per-tournament briers (same shape, all home wins).
    pooled = report.pooled["1x2"]["model_brier"]
    a = report.per_tournament["A"]["1x2"]["model_brier"]
    b = report.per_tournament["B"]["1x2"]["model_brier"]
    assert pooled == pytest.approx((2 * a + b) / 3, rel=1e-9)


def test_aggregate_skips_market_when_outcome_missing() -> None:
    full = _pred(1)
    partial = MatchPrediction(
        match_id=2,
        tournament_id="T1",
        kickoff_utc=datetime(2024, 6, 1),
        observed={"1x2": 0},  # no ou_2_5 / btts
        model_probs=full.model_probs,
        baseline_probs=full.baseline_probs,
    )
    report = aggregate([full, partial])
    assert report.pooled["1x2"]["n"] == 2
    assert report.pooled["ou_2_5"]["n"] == 1
    assert report.pooled["btts"]["n"] == 1
    # corners has zero samples — brier is nan, n=0
    assert report.pooled["corners_total_9_5"]["n"] == 0


def test_aggregate_rejects_shape_mismatch_between_model_and_baseline() -> None:
    bad = MatchPrediction(
        match_id=99,
        tournament_id="T1",
        kickoff_utc=datetime(2024, 6, 1),
        observed={"1x2": 0},
        model_probs={"1x2": np.array([0.5, 0.3, 0.2])},
        baseline_probs={"1x2": np.array([0.5, 0.5])},
    )
    with pytest.raises(ValueError, match=r"shape"):
        aggregate([bad])


# ---------------------------------------------------------------------------
# run_walk_forward
# ---------------------------------------------------------------------------


def _synthetic_corpus(rng: np.random.Generator, n_matches: int = 200) -> pd.DataFrame:
    """Build a synthetic 4-team corpus where team strengths are deterministic.

    Team A is strong, B is medium, C/D are weak — so the model has a real
    edge over a coin-flip baseline.
    """
    teams = ["A", "B", "C", "D"]
    attack = {"A": 0.6, "B": 0.2, "C": -0.3, "D": -0.5}
    defence = {"A": -0.4, "B": -0.1, "C": 0.2, "D": 0.4}
    home_adv = 0.25
    base_log = 0.4  # ≈ 1.5 goals/team baseline

    rows = []
    start = datetime(2022, 1, 1)
    for i in range(n_matches):
        h, a = rng.choice(teams, size=2, replace=False)
        lam = float(np.exp(base_log + attack[h] - defence[a] + home_adv))
        mu = float(np.exp(base_log + attack[a] - defence[h]))
        rows.append(
            {
                "home_team": h,
                "away_team": a,
                "home_goals": int(rng.poisson(lam)),
                "away_goals": int(rng.poisson(mu)),
                "kickoff_utc": start + timedelta(days=i),
            }
        )
    return pd.DataFrame(rows)


def test_run_walk_forward_produces_full_pooled_report_on_synthetic_data() -> None:
    rng = np.random.default_rng(42)
    training = _synthetic_corpus(rng, n_matches=300)
    # Held-out tournament: same 4 teams, dates after training corpus.
    tournament_start = datetime(2023, 1, 1)
    test_matches = []
    for i in range(20):
        h, a = rng.choice(["A", "B", "C", "D"], size=2, replace=False)
        lam = float(np.exp(0.4 + ({"A": 0.6, "B": 0.2, "C": -0.3, "D": -0.5}[h])))
        mu = float(np.exp(0.4 + ({"A": 0.6, "B": 0.2, "C": -0.3, "D": -0.5}[a])))
        hg, ag = int(rng.poisson(lam)), int(rng.poisson(mu))
        # Baseline: uniform-ish (deliberately bad) so model has room to win.
        test_matches.append(
            TestMatch(
                match_id=i,
                tournament_id="HELD_OUT",
                home_team=h,
                away_team=a,
                kickoff_utc=tournament_start + timedelta(hours=i),
                home_goals=hg,
                away_goals=ag,
                total_corners=None,
                baseline_probs={
                    "1x2": np.array([1 / 3, 1 / 3, 1 / 3]),
                    "ou_2_5": np.array([0.5, 0.5]),
                    "btts": np.array([0.5, 0.5]),
                },
            )
        )

    report = run_walk_forward(training_matches=training, test_matches=test_matches)
    # Pooled report has all three observable markets (corners skipped — no λs)
    for market in ("1x2", "ou_2_5", "btts"):
        assert report.pooled[market]["n"] == 20
    # corners has zero samples
    assert report.pooled["corners_total_9_5"]["n"] == 0
    # Model is structurally informed; baseline is uniform — model should win ≥3way.
    assert report.pooled["1x2"]["model_brier"] < report.pooled["1x2"]["baseline_brier"]


def test_run_walk_forward_corner_market_used_when_lambdas_provided() -> None:
    rng = np.random.default_rng(7)
    training = _synthetic_corpus(rng, n_matches=200)
    test_matches = [
        TestMatch(
            match_id=1,
            tournament_id="HELD_OUT",
            home_team="A",
            away_team="C",
            kickoff_utc=datetime(2023, 1, 2),
            home_goals=2,
            away_goals=1,
            total_corners=11,  # over 9.5
            corner_lambdas=(6.0, 4.0),
            baseline_probs={
                "1x2": np.array([1 / 3, 1 / 3, 1 / 3]),
                "ou_2_5": np.array([0.5, 0.5]),
                "btts": np.array([0.5, 0.5]),
                "corners_total_9_5": np.array([0.5, 0.5]),
            },
        ),
    ]
    report = run_walk_forward(training_matches=training, test_matches=test_matches)
    assert report.pooled["corners_total_9_5"]["n"] == 1


def test_run_walk_forward_raises_when_no_training_data_predates_tournament() -> None:
    training = _synthetic_corpus(np.random.default_rng(0), n_matches=20)
    # Tournament starts before any training row exists.
    test_matches = [
        TestMatch(
            match_id=1,
            tournament_id="EARLY",
            home_team="A",
            away_team="B",
            kickoff_utc=datetime(2000, 1, 1),
            home_goals=1,
            away_goals=0,
            total_corners=None,
            baseline_probs={
                "1x2": np.array([1 / 3, 1 / 3, 1 / 3]),
                "ou_2_5": np.array([0.5, 0.5]),
                "btts": np.array([0.5, 0.5]),
            },
        )
    ]
    with pytest.raises(ValueError, match=r"no training data"):
        run_walk_forward(training_matches=training, test_matches=test_matches)


# ---------------------------------------------------------------------------
# Report rendering + sidecar round-trip
# ---------------------------------------------------------------------------


def test_render_markdown_contains_verdict_and_pooled_table() -> None:
    # Synthesize a passing report by hand.
    rows = [_pred(i, model_home=0.95) for i in range(5)]
    report = aggregate(rows)
    # Force corners populated so the acceptance check is well-formed.
    report.pooled["corners_total_9_5"] = {
        "model_brier": 0.18,
        "baseline_brier": 0.20,
        "n": 5,
    }
    md = render_markdown(report, generated_at=datetime(2026, 6, 5, 12, 0))
    assert "Backtest — Phase 0" in md
    assert "**Acceptance**" in md
    assert "`1x2`" in md
    assert "`corners_total_9_5`" in md


def test_write_reports_json_sidecar_is_acceptance_loadable(tmp_path: Path) -> None:
    rows = [_pred(i, model_home=0.95) for i in range(5)]
    report = aggregate(rows)
    report.pooled["corners_total_9_5"] = {
        "model_brier": 0.18,
        "baseline_brier": 0.20,
        "n": 5,
    }
    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    write_reports(report, md_path=md_path, json_path=json_path)

    reloaded = load_report(json_path)
    assert set(reloaded.pooled) == set(MARKETS)
    result = check(reloaded)
    assert result.passed


def test_to_json_payload_round_trip_preserves_per_tournament() -> None:
    rows = [_pred(1, tournament="A"), _pred(2, tournament="B")]
    report = aggregate(rows)
    payload = to_json_payload(report)
    assert "pooled" in payload and "per_tournament" in payload
    assert set(payload["per_tournament"]) == {"A", "B"}

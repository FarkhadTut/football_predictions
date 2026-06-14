"""Walk-forward backtest harness — REQ-007.

Two layers
----------
* :func:`aggregate` — pure aggregator. Given per-match predictions, baseline
  probabilities, and observed outcomes, produces a :class:`BacktestReport`
  with per-tournament + pooled Brier scores per market. Easy to unit-test.
* :func:`run_walk_forward` — wires Dixon-Coles refits to the aggregator.
  For each tournament: fits DC on the training corpus + matches preceding
  the tournament's first kickoff, predicts each test match's joint score
  matrix, derives 1X2 / O-U 2.5 / BTTS marginals, and (when corner λs are
  supplied) the corners-total marginal. Returns the same report shape.

The CLI :func:`main` wires this to the DB + odds layer and writes the
``reports/backtest-phase0.{md,json}`` pair so the acceptance test
(:mod:`predictor.backtest.acceptance`) can verify the gate without
re-running the model.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from predictor.backtest.acceptance import (
    MARKETS,
    BacktestReport,
    check,
)
from predictor.backtest.metrics import brier_score, reliability
from predictor.model.dixon_coles import DixonColesModel
from predictor.model.markets import corner_total_prob_at_least, from_score_matrix

logger = logging.getLogger(__name__)

# Outcome index conventions (must match the column order in `probs` matrices).
_H2H_INDEX = {"home": 0, "draw": 1, "away": 2}
_BINARY_INDEX = {"yes": 0, "no": 1, "over": 0, "under": 1}


@dataclass(frozen=True)
class MatchPrediction:
    """One row consumed by :func:`aggregate`.

    Attributes
    ----------
    match_id
        Stable identifier (used only for error messages and reproducibility).
    tournament_id
        Bucket key for per-tournament reporting (e.g. ``"EURO_2024"``).
    kickoff_utc
        Naive-UTC kickoff timestamp — used by :func:`run_walk_forward` to
        order matches but otherwise opaque to :func:`aggregate`.
    observed
        ``{market: outcome_index}``. Markets with no observation are skipped
        from that market's score (e.g. corner outcome missing). Indices follow
        :data:`_H2H_INDEX` / :data:`_BINARY_INDEX`.
    model_probs
        ``{market: np.ndarray}`` with rows summing to 1.0. Length matches the
        market's outcome cardinality (3 for 1X2, 2 for the others).
    baseline_probs
        Same shape as ``model_probs``; the de-vigged implied-odds baseline.
    """

    match_id: int
    tournament_id: str
    kickoff_utc: datetime
    observed: Mapping[str, int]
    model_probs: Mapping[str, np.ndarray]
    baseline_probs: Mapping[str, np.ndarray]


def aggregate(predictions: Sequence[MatchPrediction]) -> BacktestReport:
    """Pure aggregator — group predictions by tournament, score per market.

    Markets with zero samples in a bucket produce a `nan` brier; the pooled
    section is required to have all four markets populated for the
    acceptance gate to be applicable.
    """
    # Bucket index: per (tournament, market) collect aligned probability rows
    # for model + baseline plus the observed outcome index.
    pooled_buckets: dict[str, _MarketBucket] = {m: _MarketBucket() for m in MARKETS}
    per_tournament_buckets: dict[str, dict[str, _MarketBucket]] = {}

    for row in predictions:
        for market in MARKETS:
            if market not in row.observed:
                continue
            if market not in row.model_probs or market not in row.baseline_probs:
                continue
            obs = int(row.observed[market])
            model_p = np.asarray(row.model_probs[market], dtype=np.float64)
            base_p = np.asarray(row.baseline_probs[market], dtype=np.float64)
            if model_p.shape != base_p.shape:
                raise ValueError(
                    f"match {row.match_id} market {market}: model/baseline shape "
                    f"mismatch {model_p.shape} vs {base_p.shape}"
                )
            pooled_buckets[market].add(model_p, base_p, obs)
            per_tournament_buckets.setdefault(row.tournament_id, {}).setdefault(
                market, _MarketBucket()
            ).add(model_p, base_p, obs)

    pooled = {m: pooled_buckets[m].score() for m in MARKETS}
    per_tournament = {
        t: {m: b.score() for m, b in markets.items()}
        for t, markets in per_tournament_buckets.items()
    }
    return BacktestReport(pooled=pooled, per_tournament=per_tournament)


@dataclass
class _MarketBucket:
    model_rows: list[np.ndarray] = field(default_factory=list)
    baseline_rows: list[np.ndarray] = field(default_factory=list)
    outcomes: list[int] = field(default_factory=list)

    def add(self, model_p: np.ndarray, base_p: np.ndarray, outcome: int) -> None:
        self.model_rows.append(model_p)
        self.baseline_rows.append(base_p)
        self.outcomes.append(outcome)

    def score(self) -> dict[str, float]:
        n = len(self.outcomes)
        if n == 0:
            return {"model_brier": float("nan"), "baseline_brier": float("nan"), "n": 0}
        model = np.vstack(self.model_rows)
        baseline = np.vstack(self.baseline_rows)
        obs = np.asarray(self.outcomes, dtype=np.int64)
        return {
            "model_brier": brier_score(model, obs),
            "baseline_brier": brier_score(baseline, obs),
            "n": n,
        }


# ---------------------------------------------------------------------------
# Walk-forward harness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestMatch:
    """One held-out tournament fixture fed to the walk-forward runner.

    ``corner_lambdas`` is the per-team Poisson rate pair used for the corners
    market when corner odds (and observed corner totals) are available. If
    omitted, the corners market is skipped for this match.
    """

    # Tell pytest this is not a test class (the ``Test`` prefix is unrelated).
    __test__ = False

    match_id: int
    tournament_id: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    home_goals: int
    away_goals: int
    total_corners: int | None
    baseline_probs: Mapping[str, np.ndarray]
    corner_lambdas: tuple[float, float] | None = None


def run_walk_forward(
    *,
    training_matches: pd.DataFrame,
    test_matches: Sequence[TestMatch],
    half_life_days: float | None = None,
    max_goals: int = 10,
    corners_line: float = 9.5,
) -> BacktestReport:
    """Refit DC per tournament, score every test match, aggregate.

    ``training_matches`` is the full historical corpus (must contain the
    columns required by :meth:`DixonColesModel.fit`). For each unique
    tournament in ``test_matches``, the model is fit on rows of
    ``training_matches`` with ``kickoff_utc`` strictly before the first test
    kickoff in that tournament — this is the walk-forward boundary.
    """
    if "kickoff_utc" not in training_matches.columns:
        raise ValueError("training_matches must contain kickoff_utc")

    # Bucket test matches by tournament to fit once per tournament.
    by_tournament: dict[str, list[TestMatch]] = {}
    for tm in test_matches:
        by_tournament.setdefault(tm.tournament_id, []).append(tm)

    predictions: list[MatchPrediction] = []
    skipped_unknown = 0
    for tournament_id, matches in by_tournament.items():
        first_kickoff = min(m.kickoff_utc for m in matches)
        train_mask = pd.to_datetime(training_matches["kickoff_utc"]) < pd.Timestamp(first_kickoff)
        train = training_matches.loc[train_mask]
        if train.empty:
            raise ValueError(
                f"no training data before tournament {tournament_id} start {first_kickoff}"
            )
        model = DixonColesModel(half_life_days=half_life_days)
        model.fit(train, as_of=first_kickoff)
        params = model.params
        assert params is not None  # fit() populates params
        known = set(params.teams)
        for tm in matches:
            # A team making its tournament debut has no pre-tournament training
            # row, so the model never saw it. Skip rather than crash; these are
            # rare and excluding them keeps the walk-forward honest.
            if tm.home_team not in known or tm.away_team not in known:
                skipped_unknown += 1
                logger.warning(
                    "backtest skip (unseen team): %s vs %s in %s",
                    tm.home_team,
                    tm.away_team,
                    tournament_id,
                )
                continue
            predictions.append(
                _predict_one(
                    tm,
                    model=model,
                    max_goals=max_goals,
                    corners_line=corners_line,
                )
            )

    if skipped_unknown:
        logger.warning(
            "backtest skipped %d test match(es) with unseen teams", skipped_unknown
        )
    return aggregate(predictions)


def _predict_one(
    tm: TestMatch,
    *,
    model: DixonColesModel,
    max_goals: int,
    corners_line: float,
) -> MatchPrediction:
    score_matrix = model.predict(tm.home_team, tm.away_team, max_goals=max_goals)
    marginals = from_score_matrix(score_matrix)
    model_probs: dict[str, np.ndarray] = {
        "1x2": np.array([marginals.p_home, marginals.p_draw, marginals.p_away]),
        "ou_2_5": np.array([marginals.p_over_2_5, marginals.p_under_2_5]),
        "btts": np.array([marginals.p_btts_yes, marginals.p_btts_no]),
    }

    observed: dict[str, int] = {
        "1x2": _outcome_1x2(tm.home_goals, tm.away_goals),
        "ou_2_5": 0 if (tm.home_goals + tm.away_goals) > 2 else 1,
        "btts": 0 if (tm.home_goals >= 1 and tm.away_goals >= 1) else 1,
    }

    if tm.corner_lambdas is not None and tm.total_corners is not None:
        lam_h, lam_a = tm.corner_lambdas
        line_int = math.floor(corners_line) + 1  # 9.5 → "≥ 10"
        p_over = corner_total_prob_at_least(lam_h, lam_a, line_int)
        model_probs["corners_total_9_5"] = np.array([p_over, 1.0 - p_over])
        observed["corners_total_9_5"] = 0 if tm.total_corners > corners_line else 1

    return MatchPrediction(
        match_id=tm.match_id,
        tournament_id=tm.tournament_id,
        kickoff_utc=tm.kickoff_utc,
        observed=observed,
        model_probs=model_probs,
        baseline_probs=tm.baseline_probs,
    )


def _outcome_1x2(home_goals: int, away_goals: int) -> int:
    if home_goals > away_goals:
        return _H2H_INDEX["home"]
    if home_goals < away_goals:
        return _H2H_INDEX["away"]
    return _H2H_INDEX["draw"]


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


_MARKET_OUTCOME_LABELS: dict[str, tuple[str, ...]] = {
    "1x2": ("home", "draw", "away"),
    "ou_2_5": ("over", "under"),
    "btts": ("yes", "no"),
    "corners_total_9_5": ("over", "under"),
}


def to_json_payload(report: BacktestReport) -> dict[str, Any]:
    """JSON-serialisable mirror of :class:`BacktestReport`."""
    return {
        "pooled": report.pooled,
        "per_tournament": report.per_tournament,
    }


def render_markdown(
    report: BacktestReport,
    *,
    reliability_per_market: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render a human-readable markdown report.

    ``reliability_per_market`` is an optional mapping of pre-rendered diagram
    snippets (markdown tables) keyed by market id. The CLI passes the output
    of :func:`reliability_table` for each market; tests can pass an empty
    mapping to keep snapshots focused on the Brier table.
    """
    reliability_per_market = reliability_per_market or {}
    ts = (generated_at or datetime.now()).isoformat(timespec="seconds")
    result = check(report)
    lines = [
        "# Backtest — Phase 0",
        "",
        f"_Generated: {ts}_",
        "",
        f"**Acceptance**: {'PASS' if result.passed else 'FAIL'} "
        f"({len(result.passing_markets)}/4 markets at ratio ≤ 0.98)",
        "",
        "## Pooled Brier (across all held-out tournaments)",
        "",
        "| Market | model | baseline | ratio | n | verdict |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for m in MARKETS:
        row = report.pooled[m]
        ratio = result.ratios[m]
        verdict = "pass" if m in result.passing_markets else "fail"
        lines.append(
            f"| `{m}` | {row['model_brier']:.4f} | {row['baseline_brier']:.4f} "
            f"| {ratio:.4f} | {int(row.get('n', 0))} | {verdict} |"
        )
    lines.append("")
    if report.per_tournament:
        lines.append("## Per tournament")
        lines.append("")
        for tournament, markets in sorted(report.per_tournament.items()):
            lines.append(f"### {tournament}")
            lines.append("")
            lines.append("| Market | model | baseline | n |")
            lines.append("|---|---:|---:|---:|")
            for m in MARKETS:
                t_row = markets.get(m)
                if t_row is None:
                    lines.append(f"| `{m}` | — | — | 0 |")
                    continue
                lines.append(
                    f"| `{m}` | {t_row['model_brier']:.4f} | {t_row['baseline_brier']:.4f} "
                    f"| {int(t_row.get('n', 0))} |"
                )
            lines.append("")
    if reliability_per_market:
        lines.append("## Reliability diagrams")
        lines.append("")
        for m in MARKETS:
            block = reliability_per_market.get(m)
            if block is None:
                continue
            lines.append(f"### `{m}`")
            lines.append("")
            lines.append(block)
            lines.append("")
    return "\n".join(lines)


def reliability_table(probs: np.ndarray, outcomes: np.ndarray, *, n_bins: int = 10) -> str:
    """Render a reliability diagram as a markdown table."""
    r = reliability(probs, outcomes, n_bins=n_bins)
    lines = [
        f"_ECE = {r.ece:.4f}_",
        "",
        "| bin | range | pred | freq | n |",
        "|---:|:---|---:|---:|---:|",
    ]
    for i in range(n_bins):
        lo, hi = float(r.bin_edges[i]), float(r.bin_edges[i + 1])
        lines.append(
            f"| {i} | [{lo:.2f}, {hi:.2f}{')' if i < n_bins - 1 else ']'} "
            f"| {float(r.bin_pred[i]):.3f} | {float(r.bin_freq[i]):.3f} "
            f"| {int(r.bin_counts[i])} |"
        )
    return "\n".join(lines)


def write_reports(
    report: BacktestReport,
    *,
    md_path: Path,
    json_path: Path,
    reliability_per_market: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
) -> None:
    """Write the markdown + JSON sidecar pair to disk."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        render_markdown(
            report,
            reliability_per_market=reliability_per_market,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(to_json_payload(report), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point — not invoked by tests; thin wrapper over the harness.

    The wiring to the DB (loading training matches, baseline odds, observed
    outcomes) lives behind the lazy import so unit tests of the pure harness
    do not pull in SQLModel.
    """
    parser = argparse.ArgumentParser(prog="predictor.backtest.run")
    parser.add_argument(
        "--report",
        default="reports/backtest-phase0.md",
        help="markdown report output (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        default="reports/backtest-phase0.json",
        help="machine-readable sidecar output (default: %(default)s)",
    )
    parser.add_argument(
        "--half-life-days",
        type=float,
        default=None,
        help="exponential time-weighting half-life (default: uniform weights)",
    )
    args = parser.parse_args(argv)

    # Lazy import — keeps unit tests of aggregate/render free of DB deps.
    # The dataset adapter wires this CLI to the ingest tables; it lives in a
    # separate module so the pure aggregator stays test-isolated.
    from predictor.backtest.dataset import (
        load_test_matches,
        load_training_matches,
    )

    training = load_training_matches()
    test_matches = load_test_matches()
    report = run_walk_forward(
        training_matches=training,
        test_matches=test_matches,
        half_life_days=args.half_life_days,
    )
    write_reports(report, md_path=Path(args.report), json_path=Path(args.json))
    result = check(report)
    print(result.summary())
    return 0 if result.passed else 1


__all__ = [
    "MARKETS",
    "MatchPrediction",
    "TestMatch",
    "aggregate",
    "reliability_table",
    "render_markdown",
    "run_walk_forward",
    "to_json_payload",
    "write_reports",
]


if __name__ == "__main__":
    raise SystemExit(_main())

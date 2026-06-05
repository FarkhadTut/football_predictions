"""Executable acceptance gate for the Phase 0 walk-forward backtest (REQ-007).

The gate is intentionally permissive on *which* market may fail (1X2 is the
hardest), but strict on the threshold (≥2% Brier reduction over the implied-
odds baseline). Implementation mirrors the pseudocode in the task doc so any
silent change to ``THRESHOLD`` or ``MIN_MARKETS_PASSING`` breaks the lock-in
test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKETS: tuple[str, ...] = ("1x2", "ou_2_5", "btts", "corners_total_9_5")
THRESHOLD: float = 0.98  # model must be ≥ 2% better than baseline
MIN_MARKETS_PASSING: int = 3


@dataclass(frozen=True)
class BacktestReport:
    """Machine-readable view of ``reports/backtest-phase0.json``.

    ``pooled`` is ``{market: {"model_brier": float, "baseline_brier": float}}``
    across all held-out tournaments. ``per_tournament`` is the per-tournament
    breakdown keyed by tournament id; the acceptance gate only inspects the
    pooled section but the field is preserved so callers can render per-
    tournament tables without re-parsing the JSON.
    """

    pooled: dict[str, dict[str, float]]
    per_tournament: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    ratios: dict[str, float]
    passing_markets: set[str]
    failing_markets: set[str]

    def summary(self) -> str:
        lines = [f"acceptance: {'PASS' if self.passed else 'FAIL'}"]
        for m in MARKETS:
            ratio = self.ratios.get(m, float("nan"))
            verdict = "pass" if m in self.passing_markets else "fail"
            lines.append(f"  {m:<22} ratio={ratio:.4f}  {verdict}")
        return "\n".join(lines)


def check(report: BacktestReport) -> AcceptanceResult:
    """Apply the REQ-007 gate to a pooled backtest report.

    Raises :class:`ValueError` if any of the four required markets is missing
    from the pooled section — the gate must not silently pass on a partial
    report.
    """
    missing = [m for m in MARKETS if m not in report.pooled]
    if missing:
        raise ValueError(f"pooled report missing markets: {missing}")

    ratios: dict[str, float] = {}
    for m in MARKETS:
        row = report.pooled[m]
        baseline = float(row["baseline_brier"])
        model = float(row["model_brier"])
        if baseline <= 0.0:
            raise ValueError(f"baseline_brier for {m} must be > 0, got {baseline}")
        ratios[m] = model / baseline

    passing = {m for m, r in ratios.items() if r <= THRESHOLD}
    return AcceptanceResult(
        passed=len(passing) >= MIN_MARKETS_PASSING,
        ratios=ratios,
        passing_markets=passing,
        failing_markets=set(MARKETS) - passing,
    )


def load_report(path: Path) -> BacktestReport:
    """Parse a ``reports/backtest-phase0.json`` sidecar into a :class:`BacktestReport`."""
    raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return BacktestReport(
        pooled=raw.get("pooled", {}),
        per_tournament=raw.get("per_tournament", {}),
    )

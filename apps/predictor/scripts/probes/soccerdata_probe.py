"""Probe `soccerdata` FBref access from this network.

Goal (per tech-decomposition Step 0.1):
  - one tournament pull (Euro 2024) -> does it work, is corners populated?
  - one club season pull (EPL 2023-24) -> same
  - record Cloudflare behaviour, latency, and column coverage

Usage:
  uv run --with soccerdata --with python-dotenv python apps/predictor/scripts/probes/soccerdata_probe.py
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # apps/predictor
REPORT_PATH = ROOT / "reports" / "probes-soccerdata.json"


def _probe(label: str, fn) -> dict[str, object]:
    entry: dict[str, object] = {"label": label}
    t0 = time.perf_counter()
    try:
        df = fn()
        entry["status"] = "ok"
        entry["rows"] = int(df.shape[0])
        entry["cols"] = int(df.shape[1])
        entry["sample_columns"] = list(map(str, df.columns[:30]))
        # corners are usually named CK or "corner_kicks" depending on stat_type
        cols_lower = " ".join(map(str, df.columns)).lower()
        entry["has_corners_signal"] = any(t in cols_lower for t in ("corner", "ck"))
    except Exception as exc:  # noqa: BLE001 — probe records *any* failure
        entry["status"] = "error"
        entry["error_type"] = type(exc).__name__
        entry["error_msg"] = str(exc)[:500]
        # quick Cloudflare smell test
        msg = str(exc).lower()
        entry["cloudflare_suspected"] = any(
            tok in msg for tok in ("cloudflare", "403", "1020", "challenge", "captcha")
        )
        entry["traceback_tail"] = traceback.format_exc().splitlines()[-3:]
    entry["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return entry


def main() -> int:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    findings: dict[str, object] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "probes": [],
    }

    # Import inside so the file is at least syntactically importable without
    # soccerdata installed (we run via `uv run --with soccerdata`).
    import soccerdata as sd  # noqa: PLC0415

    findings["soccerdata_version"] = getattr(sd, "__version__", "unknown")

    # 1) International tournament — Euro 2024
    def euro_2024():
        fbref = sd.FBref(leagues="INT-European Championship", seasons=2024)
        # schedule is the cheapest endpoint and almost always works first
        return fbref.read_schedule()

    findings["probes"].append(_probe("fbref-euro-2024-schedule", euro_2024))

    # 2) Club season — EPL 2023-24 team match stats with corners signal
    def epl_2023_24():
        fbref = sd.FBref(leagues="ENG-Premier League", seasons="2023-2024")
        return fbref.read_team_match_stats(stat_type="passing_types")

    findings["probes"].append(_probe("fbref-epl-2023-24-team-stats", epl_2023_24))

    REPORT_PATH.write_text(json.dumps(findings, indent=2, default=str))
    print(f"OK: wrote {REPORT_PATH.relative_to(ROOT.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

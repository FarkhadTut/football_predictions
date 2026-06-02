"""Probe the-odds-api WC 2026 coverage.

Goal (per tech-decomposition Step 0.1):
  - confirm `soccer_fifa_world_cup` is exposed
  - record which of {h2h, totals, btts, corners} have prices for upcoming fixtures
  - capture a sample payload for respx fixtures
  - record remaining monthly quota

Usage:
  uv run --with httpx --with python-dotenv python apps/predictor/scripts/probes/the_odds_api_probe.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # apps/predictor
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "odds_api"
REPORT_PATH = ROOT / "reports" / "probes-the-odds-api.json"

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "soccer_fifa_world_cup"
# Markets to probe individually so we can record exactly which are supported.
# the-odds-api spec: h2h, totals, btts come on the free tier; corners are alt
# markets and may require a paid plan or simply be missing for the sport.
PROBE_MARKETS = ["h2h", "totals", "btts", "alternate_totals_corners"]


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        print("FATAL: THE_ODDS_API_KEY missing from environment", file=sys.stderr)
        return 2

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    findings: dict[str, object] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "sport_key": SPORT_KEY,
        "probes": {},
    }

    with httpx.Client(timeout=15.0) as client:
        # 1) sport availability
        r = client.get(
            f"{BASE}/sports",
            params={"apiKey": api_key, "all": "true"},
        )
        findings["sports_status"] = r.status_code
        findings["quota_remaining"] = r.headers.get("x-requests-remaining")
        findings["quota_used"] = r.headers.get("x-requests-used")
        if r.status_code != 200:
            findings["sports_error"] = r.text[:500]
            _write(findings)
            return 1

        sports = r.json()
        wc = next((s for s in sports if s.get("key") == SPORT_KEY), None)
        findings["sport_exposed"] = wc is not None
        if wc:
            findings["sport_details"] = {k: wc.get(k) for k in ("title", "active", "has_outrights", "group")}

        # 2) per-market odds probe — one call each so coverage is unambiguous
        for market in PROBE_MARKETS:
            resp = client.get(
                f"{BASE}/sports/{SPORT_KEY}/odds",
                params={
                    "apiKey": api_key,
                    "regions": "eu,uk",
                    "markets": market,
                    "oddsFormat": "decimal",
                },
            )
            entry: dict[str, object] = {
                "status": resp.status_code,
                "quota_remaining": resp.headers.get("x-requests-remaining"),
            }
            if resp.status_code == 200:
                payload = resp.json()
                entry["fixture_count"] = len(payload)
                entry["bookmaker_count_first"] = (
                    len(payload[0].get("bookmakers", [])) if payload else 0
                )
                # save the first payload we get to use as a respx fixture
                fixture_file = FIXTURE_DIR / f"{market}.json"
                fixture_file.write_text(json.dumps(payload[:3], indent=2))
                entry["fixture_saved"] = str(fixture_file.relative_to(ROOT.parent.parent))
            else:
                entry["error"] = resp.text[:500]
            findings["probes"][market] = entry

    _write(findings)
    print(f"OK: wrote {REPORT_PATH.relative_to(ROOT.parent.parent)}")
    return 0


def _write(findings: dict[str, object]) -> None:
    REPORT_PATH.write_text(json.dumps(findings, indent=2))


if __name__ == "__main__":
    sys.exit(main())

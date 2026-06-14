"""Ingest the Phase 0 tournament catalog into the configured DB.

Runs the six historical national-team tournaments through
``predictor.ingest.tournaments.load_tournament`` using the live
``FBrefTournamentSource``. Idempotent — re-running upserts.

Run with::

    uv run python -m scripts.ingest_tournaments

or::

    uv run python scripts/ingest_tournaments.py
"""

from __future__ import annotations

import logging
import sys
import time

from predictor.db.session import get_session
from predictor.ingest.fbref_source import FBrefTournamentSource
from predictor.ingest.tournaments import load_tournament

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ingest_tournaments")

# (friendly name, season). Season strings flow through to soccerdata verbatim.
TOURNAMENTS: list[tuple[str, str]] = [
    ("Euro 2016", "2016"),
    ("Euro 2020", "2020"),
    ("Euro 2024", "2024"),
    ("WC 2014", "2014"),
    ("WC 2018", "2018"),
    ("WC 2022", "2022"),
]


def main() -> int:
    # FBref is Cloudflare-blocked from this IP; read soccerdata's local
    # HTML cache only. A season with no cached files surfaces as a clean
    # FileNotFoundError (logged + recorded), not a hang.
    source = FBrefTournamentSource(offline=True)
    failures: list[tuple[str, str, str]] = []
    for name, season in TOURNAMENTS:
        logger.info("---- %s (%s) ----", name, season)
        t0 = time.time()
        try:
            with get_session() as session:
                result = load_tournament(session, source, name, season)
            elapsed = time.time() - t0
            logger.info(
                "%s %s: matches +%d/~%d  stats +%d/~%d  teams +%d  (%.1fs)",
                name,
                season,
                result.matches_added,
                result.matches_updated,
                result.stats_added,
                result.stats_updated,
                result.teams_added,
                elapsed,
            )
        except Exception as exc:
            logger.exception("FAILED %s %s: %s", name, season, exc)
            failures.append((name, season, str(exc)))
    if failures:
        logger.error("Failures: %s", failures)
        return 1
    logger.info("All tournaments ingested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Ingest historical OddsPortal odds into ``odds_snapshots``.

Renders each tournament's results pages (browser, cached to disk), parses 1X2
odds, and upserts them against the matches already loaded by
``ingest_tournaments``. Idempotent — re-running serves cached HTML and skips
existing odds rows.

Run with::

    uv run python scripts/ingest_oddsportal.py

Pass ``--offline`` to fail on any cache miss instead of rendering (used once the
cache is warm, e.g. in CI / re-runs).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from predictor.config import get_settings
from predictor.db.session import get_session
from predictor.odds.oddsportal.load import load_oddsportal
from predictor.odds.oddsportal.render import RenderedCache
from predictor.odds.oddsportal.source import TOURNAMENTS, OddsPortalSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ingest_oddsportal")

# Fixed ingest stamp: these are historical closing-ish odds, not a live series,
# so one deterministic timestamp keeps the natural key stable across re-runs.
FETCHED_AT = datetime(2026, 6, 14, 0, 0, 0)


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    settings = get_settings()
    cache = RenderedCache(settings.oddsportal_cache_dir, offline=offline)
    source = OddsPortalSource(cache)
    failures: list[tuple[str, str]] = []
    try:
        for name in TOURNAMENTS:
            logger.info("---- %s ----", name)
            try:
                with get_session() as session:
                    result = load_oddsportal(session, source, name, fetched_at=FETCHED_AT)
                logger.info(
                    "%s: resolved %d / unmatched %d  | odds +%d (skipped %d)",
                    name,
                    result.matches_resolved,
                    result.matches_unmatched,
                    result.rows_added,
                    result.rows_skipped_existing,
                )
            except Exception as exc:
                logger.exception("FAILED %s: %s", name, exc)
                failures.append((name, str(exc)))
    finally:
        cache.close()
    if failures:
        logger.error("Failures: %s", failures)
        return 1
    logger.info("OddsPortal ingest complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

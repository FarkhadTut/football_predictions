"""Merge 3-letter country-code ``teams`` rows into their full-name canonical row.

One-off, idempotent data cleanup for the BRA/ARG/ENG/FRA duplicates seeded by
ad-hoc WC 2026 demo fixtures. Safe to re-run.

    uv run python scripts/fix_team_code_dupes.py
"""

from __future__ import annotations

import logging
import sys

from predictor.db.session import get_session
from predictor.ingest.team_dedup import merge_team_codes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fix_team_code_dupes")


def main() -> int:
    with get_session() as session:
        result = merge_team_codes(session)
    logger.info(
        "merged=%s renamed=%s | matches repointed %d (deleted dupes %d) | "
        "stats repointed %d (deleted dupes %d) | teams deleted %d",
        result.merged,
        result.renamed,
        result.matches_repointed,
        result.matches_deleted_as_dupe,
        result.stats_repointed,
        result.stats_deleted_as_dupe,
        result.teams_deleted,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

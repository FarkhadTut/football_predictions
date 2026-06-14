"""Merge duplicate ``teams`` rows that use a 3-letter code into the canonical
full-name team.

Some ad-hoc WC 2026 demo fixtures were seeded with country *codes* (``BRA``,
``ARG``, ``ENG``, ``FRA``) instead of the full names the FBref loader uses
(``Brazil``, ``Argentina`` …). That splits a country across two ``teams`` rows,
so a WC 2026 fixture won't resolve against the historical team the model was
trained on. This repoints every FK from the coded team onto the canonical team
and deletes the now-orphaned coded row.

Idempotent: re-running is a no-op once the coded rows are gone. Collision-safe:
if repointing would duplicate a match (natural key) or a per-team stat, the
coded-side row is dropped instead of violating the unique constraint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from predictor.db.models import Match, MatchStat, Team

__all__ = ["CANONICAL_TEAM_CODES", "MergeResult", "merge_team_codes"]

logger = logging.getLogger(__name__)

# 3-letter code -> canonical full name (as produced by the FBref loader).
CANONICAL_TEAM_CODES: dict[str, str] = {
    "BRA": "Brazil",
    "ARG": "Argentina",
    "ENG": "England",
    "FRA": "France",
}


@dataclass
class MergeResult:
    merged: list[tuple[str, str]] = field(default_factory=list)  # (code, canonical)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # code renamed in place
    matches_repointed: int = 0
    matches_deleted_as_dupe: int = 0
    stats_repointed: int = 0
    stats_deleted_as_dupe: int = 0
    teams_deleted: int = 0


def _match_twin_exists(session: Session, m: Match, *, home_id: int, away_id: int) -> bool:
    """Does another match share the natural key after repointing?"""
    stmt = select(Match.id).where(
        Match.competition == m.competition,
        Match.season == m.season,
        Match.home_team_id == home_id,
        Match.away_team_id == away_id,
        Match.kickoff_utc == m.kickoff_utc,
        Match.id != m.id,
    )
    return session.exec(stmt).first() is not None


def merge_team_codes(
    session: Session,
    mapping: dict[str, str] = CANONICAL_TEAM_CODES,
) -> MergeResult:
    """Merge each coded team into its canonical team. Commits on success."""
    result = MergeResult()
    # A match with *both* teams coded (e.g. BRA vs ARG) is touched in two
    # iterations; count it once.
    repointed_match_ids: set[int] = set()
    for code, canonical in mapping.items():
        coded = session.exec(select(Team).where(Team.name == code)).first()
        if coded is None:
            continue  # already merged / never existed
        canon = session.exec(select(Team).where(Team.name == canonical)).first()
        if canon is None:
            # No canonical row to merge into — just rename the coded one.
            coded.name = canonical
            session.add(coded)
            result.renamed.append((code, canonical))
            logger.info("renamed team %r -> %r (no canonical row existed)", code, canonical)
            continue

        assert coded.id is not None and canon.id is not None
        # Repoint matches.
        matches = session.exec(
            select(Match).where(
                (Match.home_team_id == coded.id) | (Match.away_team_id == coded.id)
            )
        ).all()
        for m in matches:
            new_home = canon.id if m.home_team_id == coded.id else m.home_team_id
            new_away = canon.id if m.away_team_id == coded.id else m.away_team_id
            if _match_twin_exists(session, m, home_id=new_home, away_id=new_away):
                session.delete(m)
                result.matches_deleted_as_dupe += 1
            else:
                m.home_team_id = new_home
                m.away_team_id = new_away
                session.add(m)
                assert m.id is not None
                if m.id not in repointed_match_ids:
                    repointed_match_ids.add(m.id)
                    result.matches_repointed += 1

        # Repoint per-team match stats.
        stats = session.exec(select(MatchStat).where(MatchStat.team_id == coded.id)).all()
        for st in stats:
            twin = session.exec(
                select(MatchStat.id).where(
                    MatchStat.match_id == st.match_id,
                    MatchStat.team_id == canon.id,
                )
            ).first()
            if twin is not None:
                session.delete(st)
                result.stats_deleted_as_dupe += 1
            else:
                st.team_id = canon.id
                session.add(st)
                result.stats_repointed += 1

        session.delete(coded)
        result.teams_deleted += 1
        result.merged.append((code, canonical))
        logger.info("merged team %r -> %r", code, canonical)

    session.commit()
    return result

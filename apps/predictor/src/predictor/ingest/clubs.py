"""Club-match historical loader.

Pulls recent club matches (last ~3 seasons) for the players in our WC
2026 candidate pool. The source contract is symmetric to the tournament
loader: a typed ``ScheduleRow`` / ``TeamMatchStatRow`` stream that the
shared ``upsert_schedule_and_stats`` helper writes idempotently.

Why we pass FBref player ids (not local ``Player.id``):

  - The caller resolves candidates from ``squad_heuristic.candidates_for``
    before any DB writes, so local ``Player`` rows may not exist yet.
  - The source layer (FBref adapter) needs an external id it can query
    against, and FBref ids are stable across our ingest sessions.

Production adapter (``soccerdata.FBref``) is deferred until the live
smoke step; tests use a fixture-driven ``FakeClubMatchSource``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlmodel import Session

from predictor.ingest._upsert import LoadResult, upsert_schedule_and_stats
from predictor.ingest.contracts import ScheduleRow, TeamMatchStatRow

__all__ = [
    "ClubMatchSource",
    "LoadResult",
    "load_recent_club_matches",
]


class ClubMatchSource(Protocol):
    """Pluggable source for club matches involving a candidate player pool."""

    def fetch_schedule(self, player_fbref_ids: list[str], since: datetime) -> list[ScheduleRow]: ...

    def fetch_team_match_stats(
        self, player_fbref_ids: list[str], since: datetime
    ) -> list[TeamMatchStatRow]: ...


def load_recent_club_matches(
    session: Session,
    source: ClubMatchSource,
    player_fbref_ids: list[str],
    since: datetime,
) -> LoadResult:
    """Idempotently ingest recent club matches for the candidate pool.

    Matches and per-team stats are upserted on their natural keys, so
    re-running with the same input is a no-op and an updated score on
    a previously-scheduled match flips it to ``status='final'``.
    """
    schedule = source.fetch_schedule(player_fbref_ids, since)
    stats = source.fetch_team_match_stats(player_fbref_ids, since)
    result = upsert_schedule_and_stats(session, schedule, stats)
    session.commit()
    return result

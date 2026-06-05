"""Tournament historical loader.

Loads completed national-team tournaments (Euros + World Cups) into ``matches``
and ``match_stats`` with an idempotent natural-key upsert.

Design note
-----------
``soccerdata`` does its own HTTP, parsing, and disk caching, so mocking at the
HTTP layer is fragile. Instead this module defines a ``TournamentSource``
Protocol returning typed rows, and the production ``FBrefTournamentSource``
adapter is a thin wrapper that translates ``soccerdata.FBref`` DataFrames into
those rows. Tests use a ``FakeSource`` returning fixture rows directly.

Public entry point
------------------
``load_tournament(session, source, name, season) -> LoadResult`` resolves
team names → ``teams.id`` (creating rows as needed), upserts ``matches`` on
``(competition, season, home_team_id, away_team_id, kickoff_utc)``, and
upserts ``match_stats`` on ``(match_id, team_id)``. Re-running is a no-op.
"""

from __future__ import annotations

from typing import Protocol

from sqlmodel import Session

from predictor.ingest._upsert import LoadResult, upsert_schedule_and_stats
from predictor.ingest.contracts import ScheduleRow, TeamMatchStatRow

# Re-export so existing callers can keep importing row contracts from here.
__all__ = [
    "TOURNAMENT_CATALOG",
    "LoadResult",
    "ScheduleRow",
    "TeamMatchStatRow",
    "TournamentSource",
    "fbref_league_for",
    "load_tournament",
]

# ---------------------------------------------------------------------------
# Tournament catalog
# ---------------------------------------------------------------------------

# Maps our friendly tournament name → soccerdata FBref league id.
# Season strings are passed through to ``soccerdata`` verbatim by the adapter
# (callers control the format — FBref accepts ``"2024"``, ``"2018-2019"``, …).
TOURNAMENT_CATALOG: dict[str, str] = {
    "Euro 2024": "INT-European Championship",
    "Euro 2020": "INT-European Championship",
    "Euro 2016": "INT-European Championship",
    "WC 2022": "INT-World Cup",
    "WC 2018": "INT-World Cup",
    "WC 2014": "INT-World Cup",
}


def fbref_league_for(name: str) -> str:
    """Resolve a friendly tournament name to its FBref league id."""
    try:
        return TOURNAMENT_CATALOG[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown tournament {name!r}; known: {sorted(TOURNAMENT_CATALOG)}"
        ) from exc


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------


class TournamentSource(Protocol):
    """Pluggable source for tournament data. Implementations: FBref live,
    fixture-based fake for tests.
    """

    def fetch_schedule(self, name: str, season: str) -> list[ScheduleRow]: ...

    def fetch_team_match_stats(self, name: str, season: str) -> list[TeamMatchStatRow]: ...


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_tournament(
    session: Session,
    source: TournamentSource,
    name: str,
    season: str,
) -> LoadResult:
    """Idempotently ingest one tournament season into the database.

    The whole load runs inside a single session-managed transaction so a
    mid-load failure rolls back cleanly.
    """
    schedule = source.fetch_schedule(name, season)
    stats = source.fetch_team_match_stats(name, season)
    result = upsert_schedule_and_stats(session, schedule, stats)
    session.commit()
    return result

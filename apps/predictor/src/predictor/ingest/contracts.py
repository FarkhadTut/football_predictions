"""Source-neutral typed row contracts for match-data ingestion.

Tournament and club loaders both ingest schedule + per-team stats.
Implementations (FBref live, fixture-based fake) translate their native
format into these row types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScheduleRow:
    """One scheduled or completed match, source-neutral."""

    competition: str
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    home_goals: int | None
    away_goals: int | None

    @property
    def status(self) -> str:
        return (
            "final" if self.home_goals is not None and self.away_goals is not None else "scheduled"
        )


@dataclass(frozen=True)
class TeamMatchStatRow:
    """Per-team match stats keyed on the match's natural key + team name."""

    competition: str
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    team: str
    shots: int | None = None
    shots_on_target: int | None = None
    corners: int | None = None
    yellow_cards: int | None = None
    red_cards: int | None = None
    fouls: int | None = None

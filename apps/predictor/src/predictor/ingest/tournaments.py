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

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlmodel import Session, select

from predictor.db.models import Match, MatchStat, Team

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
# Typed row contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleRow:
    """One scheduled or completed tournament match, source-neutral."""

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


class TournamentSource(Protocol):
    """Pluggable source for tournament data. Implementations: FBref live,
    fixture-based fake for tests.
    """

    def fetch_schedule(self, name: str, season: str) -> list[ScheduleRow]: ...

    def fetch_team_match_stats(self, name: str, season: str) -> list[TeamMatchStatRow]: ...


# ---------------------------------------------------------------------------
# Loader result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadResult:
    teams_added: int
    matches_added: int
    matches_updated: int
    stats_added: int
    stats_updated: int


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _get_or_create_team(
    session: Session, name: str, *, country: str | None = None
) -> tuple[Team, bool]:
    existing = session.exec(select(Team).where(Team.name == name)).one_or_none()
    if existing is not None:
        return existing, False
    team = Team(name=name, country=country)
    session.add(team)
    session.flush()  # populate team.id without committing the outer transaction
    return team, True


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

    teams_added = 0
    matches_added = 0
    matches_updated = 0

    # Index resolved teams by name; cache across the load.
    team_id_by_name: dict[str, int] = {}

    def resolve_team_id(team_name: str) -> int:
        cached = team_id_by_name.get(team_name)
        if cached is not None:
            return cached
        team, created = _get_or_create_team(session, team_name)
        nonlocal teams_added
        if created:
            teams_added += 1
        assert team.id is not None  # populated by session.flush()
        team_id_by_name[team_name] = team.id
        return team.id

    # ---- matches ----
    match_id_by_key: dict[tuple[str, str, datetime, str, str], int] = {}
    for row in schedule:
        home_id = resolve_team_id(row.home_team)
        away_id = resolve_team_id(row.away_team)
        existing = session.exec(
            select(Match).where(
                Match.competition == row.competition,
                Match.season == row.season,
                Match.home_team_id == home_id,
                Match.away_team_id == away_id,
                Match.kickoff_utc == row.kickoff_utc,
            )
        ).one_or_none()
        if existing is None:
            match = Match(
                competition=row.competition,
                season=row.season,
                home_team_id=home_id,
                away_team_id=away_id,
                kickoff_utc=row.kickoff_utc,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                status=row.status,
            )
            session.add(match)
            session.flush()
            assert match.id is not None
            matches_added += 1
            match_id_by_key[
                (row.competition, row.season, row.kickoff_utc, row.home_team, row.away_team)
            ] = match.id
        else:
            updated = False
            if existing.home_goals != row.home_goals:
                existing.home_goals = row.home_goals
                updated = True
            if existing.away_goals != row.away_goals:
                existing.away_goals = row.away_goals
                updated = True
            new_status = row.status
            if existing.status != new_status:
                existing.status = new_status
                updated = True
            if updated:
                session.add(existing)
                matches_updated += 1
            assert existing.id is not None
            match_id_by_key[
                (row.competition, row.season, row.kickoff_utc, row.home_team, row.away_team)
            ] = existing.id

    # ---- stats ----
    stats_added = 0
    stats_updated = 0
    for stat in stats:
        key = (stat.competition, stat.season, stat.kickoff_utc, stat.home_team, stat.away_team)
        match_id = match_id_by_key.get(key)
        if match_id is None:
            # Stat row references a match we didn't ingest — skip rather than crash.
            continue
        team_id = resolve_team_id(stat.team)
        existing_stat = session.exec(
            select(MatchStat).where(MatchStat.match_id == match_id, MatchStat.team_id == team_id)
        ).one_or_none()
        if existing_stat is None:
            session.add(
                MatchStat(
                    match_id=match_id,
                    team_id=team_id,
                    shots=stat.shots,
                    shots_on_target=stat.shots_on_target,
                    corners=stat.corners,
                    yellow_cards=stat.yellow_cards,
                    red_cards=stat.red_cards,
                    fouls=stat.fouls,
                )
            )
            stats_added += 1
        else:
            changed = False
            for attr in (
                "shots",
                "shots_on_target",
                "corners",
                "yellow_cards",
                "red_cards",
                "fouls",
            ):
                new_val = getattr(stat, attr)
                if getattr(existing_stat, attr) != new_val:
                    setattr(existing_stat, attr, new_val)
                    changed = True
            if changed:
                session.add(existing_stat)
                stats_updated += 1

    session.commit()
    return LoadResult(
        teams_added=teams_added,
        matches_added=matches_added,
        matches_updated=matches_updated,
        stats_added=stats_added,
        stats_updated=stats_updated,
    )

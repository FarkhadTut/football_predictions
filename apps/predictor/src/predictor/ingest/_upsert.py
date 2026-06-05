"""Shared schedule-and-stats upsert core.

Tournament and club match loaders share the same natural-key upsert
shape: a stream of ``ScheduleRow`` and ``TeamMatchStatRow`` rows → idempotent
writes to ``matches`` + ``match_stats``. This module exposes a single helper
that both loaders compose on top of.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from predictor.db.models import Match, MatchStat, Team

if TYPE_CHECKING:
    from predictor.ingest.contracts import ScheduleRow, TeamMatchStatRow


@dataclass(frozen=True)
class LoadResult:
    teams_added: int
    matches_added: int
    matches_updated: int
    stats_added: int
    stats_updated: int


def _get_or_create_team(
    session: Session, name: str, *, country: str | None = None
) -> tuple[Team, bool]:
    existing = session.exec(select(Team).where(Team.name == name)).one_or_none()
    if existing is not None:
        return existing, False
    team = Team(name=name, country=country)
    session.add(team)
    session.flush()
    return team, True


def upsert_schedule_and_stats(
    session: Session,
    schedule: list[ScheduleRow],
    stats: list[TeamMatchStatRow],
) -> LoadResult:
    """Idempotently upsert matches and per-team stats.

    Match natural key: ``(competition, season, home_team_id, away_team_id, kickoff_utc)``.
    Stat natural key: ``(match_id, team_id)``.

    Caller is responsible for ``session.commit()`` (lets the caller compose
    multiple ingest steps in one transaction).
    """
    teams_added = 0
    matches_added = 0
    matches_updated = 0

    team_id_by_name: dict[str, int] = {}

    def resolve_team_id(team_name: str) -> int:
        cached = team_id_by_name.get(team_name)
        if cached is not None:
            return cached
        team, created = _get_or_create_team(session, team_name)
        nonlocal teams_added
        if created:
            teams_added += 1
        assert team.id is not None
        team_id_by_name[team_name] = team.id
        return team.id

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
        key = (row.competition, row.season, row.kickoff_utc, row.home_team, row.away_team)
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
            match_id_by_key[key] = match.id
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
            match_id_by_key[key] = existing.id

    stats_added = 0
    stats_updated = 0
    for stat in stats:
        key = (stat.competition, stat.season, stat.kickoff_utc, stat.home_team, stat.away_team)
        match_id = match_id_by_key.get(key)
        if match_id is None:
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

    return LoadResult(
        teams_added=teams_added,
        matches_added=matches_added,
        matches_updated=matches_updated,
        stats_added=stats_added,
        stats_updated=stats_updated,
    )

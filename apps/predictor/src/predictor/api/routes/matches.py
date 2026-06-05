"""``GET /matches/{id}`` — single-match detail endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlmodel import Session

from predictor.api.schemas import MatchDetail
from predictor.db.models import Match, Team
from predictor.db.session import get_engine

router = APIRouter()


@router.get("/matches/{match_id}", response_model=MatchDetail)
def get_match(match_id: int) -> MatchDetail:
    with Session(get_engine()) as session:
        match = session.get(Match, match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="match_not_found")
        home = session.get(Team, match.home_team_id)
        away = session.get(Team, match.away_team_id)
        assert home is not None and away is not None
        assert match.id is not None
        return MatchDetail(
            id=match.id,
            competition=match.competition,
            season=match.season,
            home_team=home.name,
            away_team=away.name,
            kickoff_utc=match.kickoff_utc,
            status=match.status,
            home_goals=match.home_goals,
            away_goals=match.away_goals,
        )

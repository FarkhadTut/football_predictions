"""``GET /fixtures`` — list scheduled matches sorted by kickoff."""

from __future__ import annotations

from fastapi import APIRouter
from sqlmodel import Session, select

from predictor.api.schemas import Fixture
from predictor.db.models import Match, Team
from predictor.db.session import get_engine

router = APIRouter()


@router.get("/fixtures", response_model=list[Fixture])
def list_fixtures() -> list[Fixture]:
    """Return all scheduled matches sorted by ``kickoff_utc`` ascending."""
    with Session(get_engine()) as session:
        matches = session.exec(
            select(Match).where(Match.status == "scheduled").order_by(Match.kickoff_utc)  # type: ignore[arg-type]
        ).all()
        out: list[Fixture] = []
        for m in matches:
            assert m.id is not None
            home = session.get(Team, m.home_team_id)
            away = session.get(Team, m.away_team_id)
            assert home is not None and away is not None
            out.append(
                Fixture(
                    id=m.id,
                    competition=m.competition,
                    season=m.season,
                    home_team=home.name,
                    away_team=away.name,
                    kickoff_utc=m.kickoff_utc,
                    status=m.status,
                )
            )
        return out

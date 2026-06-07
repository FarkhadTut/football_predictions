"""Pydantic schemas exposed over the HTTP surface.

These are the request/response models referenced by FastAPI routes. The
``ClaudeNote`` payload contract is shared with the TS UI and lives in the
:mod:`predictor_schemas` package (``packages/schemas/src/predictor_schemas/``);
it is re-exported below so route handlers and tests keep a single import
path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from predictor_schemas import (
    ClaudeNote,
    Delta1x2,
    DeltaBTTS,
    DeltaCornersTotal,
    DeltaOU25,
    QualitativeDelta,
)
from pydantic import BaseModel, ConfigDict

__all__ = [
    "ClaudeNote",
    "Delta1x2",
    "DeltaBTTS",
    "DeltaCornersTotal",
    "DeltaOU25",
    "Fixture",
    "MatchDetail",
    "PredictCachedResponse",
    "PredictEnqueuedResponse",
    "PredictRequest",
    "QualitativeDelta",
]

# ---------------------------------------------------------------------------
# Fixtures + matches
# ---------------------------------------------------------------------------


class Fixture(BaseModel):
    """Compact match row for ``GET /fixtures``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    competition: str
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str


class MatchDetail(Fixture):
    """``GET /matches/{id}`` payload — adds final score when known."""

    home_goals: int | None = None
    away_goals: int | None = None


# ---------------------------------------------------------------------------
# Predict endpoint
# ---------------------------------------------------------------------------


class PredictRequest(BaseModel):
    """Body for ``POST /matches/{id}/predict``."""

    model_version: str | None = None
    force_refit: bool = False


class PredictCachedResponse(BaseModel):
    """200 response — pre-existing prediction returned without re-fitting."""

    cached: Literal[True] = True
    match_id: int
    model_version: str
    model_run_id: int
    markets: dict[str, dict[str, float]]


class PredictEnqueuedResponse(BaseModel):
    """202 response — fit enqueued, follow up by ``model_run_id``."""

    cached: Literal[False] = False
    model_run_id: int
    status: Literal["running"] = "running"

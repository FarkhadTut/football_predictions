"""Pydantic schemas exposed over the HTTP surface.

These are the request/response models referenced by FastAPI routes. The
canonical ``ClaudeNote`` payload schema lives in
``packages/schemas/src/claude_note.py`` (Sub-step 7.2); the version here is
re-exported so route handlers and tests share a single import path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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
# Claude qualitative notes
# ---------------------------------------------------------------------------


class _DeltaBase(BaseModel):
    """Common fields for a per-market qualitative delta.

    ``log_odds_shift`` carries the documented sign convention: **positive
    shifts the outcome toward "yes / over / home"**.
    """

    log_odds_shift: float


class Delta1x2(_DeltaBase):
    market: Literal["1x2"] = "1x2"


class DeltaOU25(_DeltaBase):
    market: Literal["ou_2_5"] = "ou_2_5"


class DeltaBTTS(_DeltaBase):
    market: Literal["btts"] = "btts"


class DeltaCornersTotal(_DeltaBase):
    market: Literal["corners_total"] = "corners_total"


QualitativeDelta = Annotated[
    Delta1x2 | DeltaOU25 | DeltaBTTS | DeltaCornersTotal,
    Field(discriminator="market"),
]


class ClaudeNote(BaseModel):
    """Structured qualitative note authored by Claude.

    File-based ingest: callers write ``<notes_dir>/match-<id>.json`` and the
    API parses it via :meth:`model_validate_json` on read.
    """

    model_config = ConfigDict(extra="forbid")

    match_id: int
    created_at: datetime
    summary: str
    qualitative_deltas: list[QualitativeDelta]
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str]


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

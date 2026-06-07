"""Canonical schema for Claude qualitative notes (REQ-009).

File-based ingest contract: per-match notes land at
``<notes_dir>/match-<id>.json`` and are validated via
:meth:`ClaudeNote.model_validate_json`.

``qualitative_deltas`` is a discriminated union on ``market`` carrying a
``log_odds_shift: float``. **Sign convention: positive shifts the outcome
toward "yes / over / home".**
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _DeltaBase(BaseModel):
    """Common fields for a per-market qualitative delta."""

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
    """Structured qualitative note authored by Claude."""

    model_config = ConfigDict(extra="forbid")

    match_id: int
    created_at: datetime
    summary: str
    qualitative_deltas: list[QualitativeDelta]
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str]

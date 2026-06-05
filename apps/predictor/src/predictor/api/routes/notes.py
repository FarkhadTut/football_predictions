"""``GET /matches/{id}/notes`` — read a parsed Claude note from disk.

Semantics (REQ-009):
* file missing → 404 with ``{"detail": "no_note"}``
* file present, schema-valid → 200 with the parsed :class:`ClaudeNote`
* file present, schema-invalid → 422 with Pydantic validation errors
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from predictor.api.schemas import ClaudeNote
from predictor.config import get_settings

router = APIRouter()


def _note_path(notes_dir: Path, match_id: int) -> Path:
    return notes_dir / f"match-{match_id}.json"


@router.get("/matches/{match_id}/notes", response_model=ClaudeNote)
def get_match_note(match_id: int) -> ClaudeNote:
    path = _note_path(get_settings().notes_dir, match_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no_note")
    raw = path.read_bytes()
    try:
        return ClaudeNote.model_validate_json(raw)
    except ValidationError as exc:
        # Surface Pydantic's structured error list so the UI can render it.
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

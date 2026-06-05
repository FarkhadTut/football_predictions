"""FastAPI app factory (REQ-008).

``create_app()`` returns a fresh app bound to the current ``Settings`` so
tests can swap config via env + :func:`reset_settings_for_tests` without
process restart.
"""

from __future__ import annotations

from fastapi import FastAPI

from predictor.api.routes import fixtures, matches, notes, predict
from predictor.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="football-predictor",
        version="0.1.0",
        description="Phase 0 football match outcome predictor.",
    )
    app.state.settings = settings
    app.include_router(fixtures.router)
    app.include_router(matches.router)
    app.include_router(notes.router)
    app.include_router(predict.router)
    return app


# Module-level app for ``uvicorn predictor.api.main:app``.
app = create_app()

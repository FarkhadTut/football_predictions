"""FastAPI app factory (REQ-008).

``create_app()`` returns a fresh app bound to the current ``Settings`` so
tests can swap config via env + :func:`reset_settings_for_tests` without
process restart.

The app lifespan starts the Claude-notes file watcher (Sub-step 7.2) and
attaches the in-process event broker to ``app.state.notes_broker`` so the
``/events/notes`` SSE endpoint can fan out updates.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from predictor.api.notes_watcher import NotesEventBroker, watch_notes
from predictor.api.routes import fixtures, matches, notes, predict
from predictor.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broker = NotesEventBroker()
        stop = asyncio.Event()
        app.state.notes_broker = broker
        task = asyncio.create_task(
            watch_notes(resolved.notes_dir, broker, stop=stop),
            name="notes-watcher",
        )
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    app = FastAPI(
        title="football-predictor",
        version="0.1.0",
        description="Phase 0 football match outcome predictor.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    # CORS: the UI runs on a different origin (Vite dev → :5173, prod → its
    # own host) and uses `fetch` + `EventSource`. Without this middleware the
    # browser silently drops responses even though uvicorn returns them.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_allow_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    app.include_router(fixtures.router)
    app.include_router(matches.router)
    app.include_router(notes.router)
    app.include_router(predict.router)

    from predictor.api import events

    app.include_router(events.router)
    return app


# Module-level app for ``uvicorn predictor.api.main:app``.
app = create_app()

"""SSE endpoint streaming Claude notes events to the UI (REQ-011/014).

The watcher publishes to ``app.state.notes_broker``; this router subscribes
and forwards events as Server-Sent Events.

Implementation note: we emit the SSE wire format manually via a plain
``StreamingResponse`` rather than ``sse_starlette.EventSourceResponse``.
``sse_starlette`` works under ``uvicorn`` but its anyio task-group plus
ping-task plumbing interacts poorly with the synchronous ``TestClient``
portal — stream() can block indefinitely waiting for headers. The manual
encoder below is ~10 lines, has no external lifecycle dependency, and is
trivially testable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from predictor.api.notes_watcher import NoteEvent, NotesEventBroker

router = APIRouter()

# Comment line keeps the connection warm without polluting the event stream
# (clients/parsers must ignore lines starting with ``:`` per SSE spec).
_PING_PAYLOAD = b": ping\n\n"
_PING_INTERVAL_SECONDS = 15.0


def _encode(event: NoteEvent) -> bytes:
    payload = json.dumps(event.data, separators=(",", ":"))
    return f"event: {event.name}\ndata: {payload}\n\n".encode()


@router.get("/events/notes")
async def stream_notes(request: Request) -> StreamingResponse:
    broker: NotesEventBroker = request.app.state.notes_broker

    async def event_source() -> AsyncIterator[bytes]:
        # Flush an immediate comment so the response headers go out before
        # the first real event (clients consider the stream "open" once
        # they've seen any byte).
        yield _PING_PAYLOAD

        subscription = broker.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                next_task = asyncio.ensure_future(subscription.__anext__())
                try:
                    event = await asyncio.wait_for(next_task, timeout=_PING_INTERVAL_SECONDS)
                except TimeoutError:
                    yield _PING_PAYLOAD
                    continue
                except StopAsyncIteration:
                    break
                yield _encode(event)
        finally:
            await subscription.aclose()

    return StreamingResponse(event_source(), media_type="text/event-stream")

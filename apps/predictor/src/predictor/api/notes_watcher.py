"""Filesystem watcher + in-process pub/sub for Claude qualitative notes.

REQ-009/011/014/015. The :class:`NotesEventBroker` is an asyncio fan-out
queue: SSE subscribers each receive their own bounded queue and the watcher
publishes ``note.updated`` / ``note.invalid`` events as files land in
``settings.notes_dir``.

Filename convention: ``match-<id>.json``. Files that don't match are
ignored; files that match but fail :class:`ClaudeNote` validation produce a
``note.invalid`` event carrying the structured Pydantic error list (the
watcher does NOT crash on bad input — that's TEST-013's survival assertion).

Implementation note: we use a small async polling loop instead of
``watchfiles.awatch``. ``awatch`` relies on ``anyio.to_thread.run_sync``
which deadlocks under the FastAPI ``TestClient`` portal on Windows (the
first ``__anext__`` never resolves). A 200 ms ``os.scandir`` poll is more
than fast enough for human-curated note files and is portable across
platforms and test harnesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from predictor.api.schemas import ClaudeNote
from predictor.observability import get_logger

_MATCH_FILENAME = re.compile(r"^match-(\d+)\.json$")
_QUEUE_MAXSIZE = 64
_POLL_INTERVAL_SECONDS = 0.2

log = get_logger(__name__)


@dataclass(frozen=True)
class NoteEvent:
    """One SSE-bound event."""

    name: str  # "note.updated" | "note.invalid"
    data: dict[str, Any]


class NotesEventBroker:
    """Async fan-out: one publisher (watcher) → N subscribers (SSE clients)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[NoteEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: NoteEvent) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest to make room.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("notes_broker.drop", event_name=event.name)

    async def subscribe(self) -> AsyncGenerator[NoteEvent, None]:
        q: asyncio.Queue[NoteEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._subscribers.discard(q)


def _parse_match_id(path: Path) -> int | None:
    m = _MATCH_FILENAME.match(path.name)
    return int(m.group(1)) if m else None


def _process_path(path: Path) -> NoteEvent | None:
    """Pure-ish helper: read + validate, return an event (or None to skip)."""
    match_id = _parse_match_id(path)
    if match_id is None:
        return None
    bound = log.bind(service="notes_watcher", match_id=match_id, path=str(path))
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # File vanished between change-notify and read — ignore.
        return None
    except OSError as exc:
        bound.warning("notes_watcher.read_error", error=str(exc))
        return None
    try:
        note = ClaudeNote.model_validate_json(raw)
    except ValidationError as exc:
        bound.info("notes_watcher.invalid", errors=len(exc.errors()))
        return NoteEvent(
            name="note.invalid",
            data={"match_id": match_id, "errors": exc.errors()},
        )
    bound.info("notes_watcher.updated")
    return NoteEvent(name="note.updated", data=note.model_dump(mode="json"))


def _snapshot(notes_dir: Path) -> dict[str, float]:
    """Return ``{filename: mtime_ns}`` for ``match-*.json`` files."""
    snap: dict[str, float] = {}
    try:
        entries = os.scandir(notes_dir)
    except FileNotFoundError:
        return snap
    with entries as it:
        for entry in it:
            if not entry.is_file():
                continue
            if not _MATCH_FILENAME.match(entry.name):
                continue
            try:
                snap[entry.name] = entry.stat().st_mtime_ns
            except OSError:
                continue
    return snap


async def watch_notes(
    notes_dir: Path,
    broker: NotesEventBroker,
    *,
    stop: asyncio.Event,
) -> None:
    """Poll ``notes_dir`` and publish events to ``broker`` until ``stop`` is set."""
    notes_dir.mkdir(parents=True, exist_ok=True)
    log.info("notes_watcher.start", path=str(notes_dir))
    previous = _snapshot(notes_dir)
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_SECONDS)
                break
            except TimeoutError:
                pass
            current = _snapshot(notes_dir)
            for name, mtime in current.items():
                if previous.get(name) == mtime:
                    continue
                event = _process_path(notes_dir / name)
                if event is not None:
                    await broker.publish(event)
            previous = current
    except asyncio.CancelledError:
        log.info("notes_watcher.cancelled")
        raise
    finally:
        log.info("notes_watcher.stop")

"""SSE notes stream integration tests (REQ-011, TEST-013).

Verifies the file-system → watcher → broker → SSE pipeline:

* writing a valid ``match-<id>.json`` produces a ``note.updated`` event with
  the parsed payload on the ``/events/notes`` stream within the latency
  budget
* writing a schema-invalid file produces a ``note.invalid`` event carrying
  the validation errors and does NOT crash the watcher (a subsequent valid
  write is still observed)

Driven by a live :mod:`uvicorn` server in a background thread. The
in-process FastAPI ``TestClient`` and ``httpx.ASGITransport`` both buffer
streaming responses under Windows + Starlette, which makes them unsuitable
for asserting SSE timing. A real loopback HTTP server is the only reliable
mechanism for end-to-end SSE verification here.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from predictor.config import reset_settings_for_tests
from predictor.db.session import reset_engines_for_tests

# How long we let the writer race the SSE subscription before giving up.
SSE_DEADLINE_SECONDS = 4.0
# Small delay so the writer thread doesn't beat the SSE handshake.
WRITER_PRE_DELAY_SECONDS = 0.3
# REQ-011 latency budget.
REQ011_LATENCY_BUDGET_SECONDS = 2.0


def _pick_free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ServerHandle:
    def __init__(self, server: uvicorn.Server, thread: threading.Thread, port: int):
        self.server = server
        self.thread = thread
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)


def _start_server(app: object, port: int) -> _ServerHandle:
    config = uvicorn.Config(
        app,  # type: ignore[arg-type]
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the server to bind.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start within 5s")
    return _ServerHandle(server, thread, port)


@pytest.fixture
def live_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_ServerHandle, Path]]:
    db_url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    notes_dir = tmp_path / "claude_notes"
    notes_dir.mkdir()
    monkeypatch.setenv("PREDICTOR_DB_URL", db_url)
    monkeypatch.setenv("PREDICTOR_NOTES_DIR", str(notes_dir))
    reset_settings_for_tests()
    reset_engines_for_tests()

    from predictor.api.main import create_app

    app = create_app()
    port = _pick_free_port()
    handle = _start_server(app, port)
    try:
        yield handle, notes_dir
    finally:
        handle.stop()
        reset_engines_for_tests()
        reset_settings_for_tests()


def _valid_payload(match_id: int) -> dict[str, object]:
    return {
        "match_id": match_id,
        "created_at": "2026-06-11T12:00:00+00:00",
        "summary": "Brazil rested key players.",
        "qualitative_deltas": [{"market": "1x2", "log_odds_shift": 0.2}],
        "confidence": 0.8,
        "sources": ["https://example.com/brazil"],
    }


def _read_next_event(
    line_iter: Iterator[str], deadline: float
) -> tuple[str, dict[str, object]] | None:
    """Block until the next ``event:`` + ``data:`` pair arrives, or timeout."""
    event_name: str | None = None
    data: dict[str, object] | None = None
    while time.monotonic() < deadline:
        try:
            line = next(line_iter)
        except StopIteration:
            return None
        if not line:
            if event_name and data is not None:
                return event_name, data
            event_name, data = None, None
            continue
        if line.startswith(":"):
            continue  # comment / ping
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
    return None


def _spawn_delayed_write(path: Path, payload: object, *, delay: float) -> threading.Thread:
    def _run() -> None:
        time.sleep(delay)
        path.write_text(json.dumps(payload), encoding="utf-8")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_file_write_emits_note_updated_event_within_budget(
    live_server: tuple[_ServerHandle, Path],
) -> None:
    handle, notes_dir = live_server
    payload = _valid_payload(42)

    with (
        httpx.Client(base_url=handle.base_url, timeout=SSE_DEADLINE_SECONDS) as c,
        c.stream("GET", "/events/notes") as resp,
    ):
        assert resp.status_code == 200
        lines = resp.iter_lines()
        writer = _spawn_delayed_write(
            notes_dir / "match-42.json", payload, delay=WRITER_PRE_DELAY_SECONDS
        )
        start = time.monotonic()
        result = _read_next_event(lines, start + SSE_DEADLINE_SECONDS)
        elapsed = time.monotonic() - start
        writer.join(timeout=1.0)

    assert result is not None, "no SSE event received before deadline"
    event_name, data = result
    assert event_name == "note.updated"
    assert data["match_id"] == 42
    assert data["confidence"] == pytest.approx(0.8)
    # REQ-011: end-to-end latency budget.
    assert elapsed < REQ011_LATENCY_BUDGET_SECONDS, (
        f"SSE event arrived after {elapsed:.2f}s (budget {REQ011_LATENCY_BUDGET_SECONDS}s)"
    )


def test_invalid_file_emits_note_invalid_and_watcher_survives(
    live_server: tuple[_ServerHandle, Path],
) -> None:
    handle, notes_dir = live_server
    bad = _valid_payload(44)
    bad.pop("confidence")  # schema-invalid

    with (
        httpx.Client(base_url=handle.base_url, timeout=SSE_DEADLINE_SECONDS) as c,
        c.stream("GET", "/events/notes") as resp,
    ):
        assert resp.status_code == 200
        lines = resp.iter_lines()
        _spawn_delayed_write(notes_dir / "match-44.json", bad, delay=WRITER_PRE_DELAY_SECONDS)
        start = time.monotonic()
        invalid = _read_next_event(lines, start + SSE_DEADLINE_SECONDS)

        assert invalid is not None, "no note.invalid event observed"
        event_name, data = invalid
        assert event_name == "note.invalid"
        assert data["match_id"] == 44
        assert isinstance(data["errors"], list) and data["errors"]

        # Watcher must still respond to a subsequent valid write.
        good = _valid_payload(45)
        _spawn_delayed_write(notes_dir / "match-45.json", good, delay=0.1)
        good_evt = _read_next_event(lines, time.monotonic() + SSE_DEADLINE_SECONDS)
        assert good_evt is not None, "watcher stopped after invalid payload"
        assert good_evt[0] == "note.updated"
        assert good_evt[1]["match_id"] == 45

"""SQLAlchemy engine + session factory.

Engines are cached by URL so the FastAPI app, alembic, and ad-hoc scripts can
share connection state without re-parsing settings on every call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from predictor.config import get_settings

_engines: dict[str, Engine] = {}


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Create a new engine for ``url``. SQLite gets ``StaticPool`` so in-memory
    DBs survive across sessions in the same process (tests rely on this).
    """
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {"echo": echo}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    if connect_args:
        kwargs["connect_args"] = connect_args
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    """Return a cached engine for the configured ``db_url``."""
    url = get_settings().db_url
    engine = _engines.get(url)
    if engine is None:
        engine = make_engine(url)
        _engines[url] = engine
    return engine


def reset_engines_for_tests() -> None:
    """Dispose all cached engines (used between test sessions)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-managed session bound to the configured engine."""
    with Session(get_engine()) as session:
        yield session

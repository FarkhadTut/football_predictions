"""Centralized structlog JSON logging.

Every entrypoint (CLI scripts, FastAPI app, probes) must call
``configure_logging()`` exactly once at startup so that logs are
consistently structured and machine-readable.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

import structlog

_DEFAULT_LEVEL: Final[str] = "INFO"
_configured: bool = False


def configure_logging(level: str | None = None) -> None:
    """Configure stdlib logging + structlog to emit JSON to stdout.

    Idempotent: safe to call from multiple entrypoints.
    """
    global _configured
    if _configured:
        return

    resolved_level = (level or os.getenv("PREDICTOR_LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a configured logger. Calls ``configure_logging`` lazily."""
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger

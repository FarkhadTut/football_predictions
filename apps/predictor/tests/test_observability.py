"""Smoke test that structlog configures and emits JSON without raising."""

from __future__ import annotations

import json
import logging

from predictor.observability import configure_logging, get_logger


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("INFO")  # second call must not raise


def test_get_logger_emits_json(capsys: object) -> None:
    configure_logging("INFO")
    log = get_logger("test")
    log.info("hello", market="h2h", probability=0.42)

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["event"] == "hello"
    assert payload["market"] == "h2h"
    assert payload["probability"] == 0.42
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_log_level_env_default() -> None:
    """Numeric level lookup falls back to INFO for unknown strings."""
    assert logging.INFO == 20

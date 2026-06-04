"""Application configuration via pydantic-settings.

All env vars are prefixed with ``PREDICTOR_`` so this can coexist with other
tools in the same shell session. ``.env`` is loaded from ``apps/predictor/.env``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/predictor — same directory that holds .env and data/
APP_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Process-wide settings. Construct once via ``get_settings()``."""

    model_config = SettingsConfigDict(
        env_prefix="PREDICTOR_",
        env_file=APP_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = Field(
        default=f"sqlite:///{(APP_ROOT / 'data' / 'predictor.db').as_posix()}",
        description="SQLAlchemy URL. SQLite by default; tests override to a temp DB.",
    )
    notes_dir: Path = Field(
        default=APP_ROOT / "data" / "notes",
        description="Filesystem directory watched for Claude qualitative notes.",
    )
    log_level: str = Field(default="INFO")
    the_odds_api_key: str | None = Field(default=None, alias="THE_ODDS_API_KEY")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Force the next ``get_settings()`` call to rebuild from env."""
    global _settings
    _settings = None

"""SQLModel entities for the predictor database.

Phase 0 schema. Naming conventions:
- table names are plural snake_case
- timestamps are stored as ISO-8601 UTC ``datetime`` (SQLite stores TEXT)
- foreign keys are ``<entity>_id`` and always indexed
- composite uniqueness expressed via ``__table_args__`` with ``UniqueConstraint``

The 10x10 score-distribution matrix is persisted as a numpy ``.npy`` byte buffer
in ``score_distributions.matrix`` (``LargeBinary``). Encoding/decoding is the
caller's responsibility (see ``predictor.model.score_matrix``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, LargeBinary, UniqueConstraint
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# Reference entities
# ---------------------------------------------------------------------------


class Team(SQLModel, table=True):
    __tablename__ = "teams"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    country: str | None = None
    fbref_id: str | None = Field(default=None, index=True)


class Player(SQLModel, table=True):
    __tablename__ = "players"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    nation: str = Field(index=True)
    fbref_id: str | None = Field(default=None, index=True)
    position: str | None = None


class WCSquad(SQLModel, table=True):
    """A `(nation, player, source)` row. ``source`` is one of
    ``heuristic | announced | merged`` — see Step 3.2.
    """

    __tablename__ = "wc_squads"
    __table_args__ = (
        UniqueConstraint("nation", "player_id", "source", name="uq_wc_squad_nation_player_source"),
    )

    id: int | None = Field(default=None, primary_key=True)
    nation: str = Field(index=True)
    player_id: int = Field(foreign_key="players.id", index=True)
    source: str = Field(description="heuristic | announced | merged")
    as_of_date: datetime


# ---------------------------------------------------------------------------
# Match data
# ---------------------------------------------------------------------------


class Match(SQLModel, table=True):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint(
            "competition",
            "season",
            "home_team_id",
            "away_team_id",
            "kickoff_utc",
            name="uq_match_natural_key",
        ),
        Index("ix_matches_kickoff_utc", "kickoff_utc"),
    )

    id: int | None = Field(default=None, primary_key=True)
    competition: str = Field(index=True)
    season: str = Field(index=True)
    home_team_id: int = Field(foreign_key="teams.id", index=True)
    away_team_id: int = Field(foreign_key="teams.id", index=True)
    kickoff_utc: datetime
    home_goals: int | None = None
    away_goals: int | None = None
    status: str = Field(default="scheduled", description="scheduled | live | final")


class MatchStat(SQLModel, table=True):
    """Per-team match-level stats (shots, corners, cards, …)."""

    __tablename__ = "match_stats"
    __table_args__ = (UniqueConstraint("match_id", "team_id", name="uq_match_stats_match_team"),)

    id: int | None = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="matches.id", index=True)
    team_id: int = Field(foreign_key="teams.id", index=True)
    shots: int | None = None
    shots_on_target: int | None = None
    corners: int | None = None
    yellow_cards: int | None = None
    red_cards: int | None = None
    fouls: int | None = None


# ---------------------------------------------------------------------------
# Odds + market availability
# ---------------------------------------------------------------------------


class OddsSnapshot(SQLModel, table=True):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "match_id",
            "book",
            "market",
            "outcome",
            "fetched_at",
            name="uq_odds_snapshot_natural_key",
        ),
        Index("ix_odds_snapshots_match", "match_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="matches.id")
    book: str = Field(description="e.g. 'pinnacle', 'bet365', '1xbet'")
    market: str = Field(description="e.g. 'h2h', 'totals_2.5', 'btts', 'corners_9.5'")
    outcome: str = Field(description="'home' | 'draw' | 'away' | 'over' | 'under' | 'yes' | 'no'")
    decimal_odds: float
    fetched_at: datetime


class MarketAvailability(SQLModel, table=True):
    """Decision #20: when BTTS/corner markets can't be sourced (e.g. 1xbet
    Cloudflare block), record the gap so the UI can show an "indicative — no
    book" badge.
    """

    __tablename__ = "market_availability"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "market", "observed_at", name="uq_market_availability_natural_key"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="matches.id", index=True)
    market: str
    available: bool
    reason: str | None = Field(
        default=None, description="e.g. 'cloudflare_blocked', 'no_book_offers'"
    )
    observed_at: datetime


# ---------------------------------------------------------------------------
# Model outputs
# ---------------------------------------------------------------------------


class ModelRun(SQLModel, table=True):
    __tablename__ = "model_runs"

    id: int | None = Field(default=None, primary_key=True)
    model_version: str = Field(index=True)
    git_sha: str
    training_cutoff_utc: datetime
    fitter_config_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime


class Prediction(SQLModel, table=True):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "market", "outcome", "model_run_id", name="uq_prediction_natural_key"
        ),
        Index("ix_predictions_match_run", "match_id", "model_run_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="matches.id")
    market: str
    outcome: str
    probability: float
    model_run_id: int = Field(foreign_key="model_runs.id")
    computed_at: datetime


class ScoreDistribution(SQLModel, table=True):
    """10x10 joint score probability matrix stored as a numpy ``.npy`` buffer."""

    __tablename__ = "score_distributions"
    __table_args__ = (
        UniqueConstraint("match_id", "model_run_id", name="uq_score_distribution_match_run"),
        Index("ix_score_distributions_match_run", "match_id", "model_run_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="matches.id")
    model_run_id: int = Field(foreign_key="model_runs.id")
    matrix: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    computed_at: datetime


# ---------------------------------------------------------------------------
# Claude qualitative notes (file pointer only)
# ---------------------------------------------------------------------------


class ClaudeNote(SQLModel, table=True):
    """Pointer to a markdown note on disk. Payload lives in the file; the row
    tracks identity + ingestion hash so re-ingest is idempotent.
    """

    __tablename__ = "claude_notes"
    __table_args__ = (UniqueConstraint("path", "content_hash", name="uq_claude_note_path_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    match_id: int | None = Field(default=None, foreign_key="matches.id", index=True)
    path: str = Field(description="absolute or notes_dir-relative path")
    content_hash: str = Field(description="sha256 of the file contents at ingest")
    ingested_at: datetime

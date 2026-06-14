"""Source-neutral row contracts for the OddsPortal loader.

``ParsedMatch`` is what the pure DOM parser emits per match; ``OddsRow`` is the
flattened (market, outcome, price) unit the loader upserts into
``odds_snapshots``. Both are frozen so they are safe to pass around and hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = ["OddsRow", "ParsedMatch"]


@dataclass(frozen=True)
class ParsedMatch:
    """One match as parsed from an OddsPortal results-list row.

    ``detail_path`` is the OddsPortal H2H/match link (used in M2 to fetch the
    over/under and BTTS sub-pages). Score is best-effort and only used as a
    cross-check — match outcomes come from FBref, not OddsPortal.
    """

    match_date: date
    home_team: str
    away_team: str
    home_odds: float
    draw_odds: float
    away_odds: float
    detail_path: str | None = None
    home_score: int | None = None
    away_score: int | None = None


@dataclass(frozen=True)
class OddsRow:
    """A single (market, outcome) price for one match, source-neutral.

    Maps directly onto an ``odds_snapshots`` row once the match is resolved to a
    ``match_id``. ``market``/``outcome`` already use the DB vocabulary
    (``h2h``/``totals_2.5``/``btts`` and ``home``/``draw``/``away``/``over``/
    ``under``/``yes``/``no``) so the loader writes them verbatim.
    """

    competition: str
    season: str
    home_team: str
    away_team: str
    match_date: date
    market: str
    outcome: str
    decimal_odds: float

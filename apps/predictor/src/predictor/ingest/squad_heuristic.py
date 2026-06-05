"""Heuristic WC 2026 squad seeder.

Until federations publish official squads, we approximate "likely
squad members" for each nation from observed senior caps + top-5
league activity. Source data comes from a ``PlayerCapsSource``
Protocol — production wires it to ``soccerdata.FBref``, tests pass
fixture rows directly.

Rule (per phase 0 plan):
- A player is a candidate for ``nation`` as of ``as_of_date`` if EITHER
    (a) they earned >= 3 senior caps for ``nation`` in the trailing
        12 months, OR
    (b) they had >= 1 start in a top-5 league in that window AND
        earned >= 1 senior cap for ``nation`` in that window.
- Top-5 leagues match the soccerdata FBref league ids:
  ``ENG-Premier League``, ``ESP-La Liga``, ``GER-Bundesliga``,
  ``ITA-Serie A``, ``FRA-Ligue 1``.

The heuristic is intentionally generous on rule (b): we accept any
cap in the window rather than "any cap ever" because data sources
typically return windowed caps and the looser definition admits the
rare cap-then-club-form case that a strict reading would drop.
Mis-inclusions cost less than mis-exclusions for downstream model
fitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

# Soccerdata's canonical league ids for the top-5 European leagues.
TOP5_LEAGUES: frozenset[str] = frozenset(
    {
        "ENG-Premier League",
        "ESP-La Liga",
        "GER-Bundesliga",
        "ITA-Serie A",
        "FRA-Ligue 1",
    }
)

# Window for "recent" caps + league activity.
HEURISTIC_WINDOW = timedelta(days=365)


# ---------------------------------------------------------------------------
# Typed row contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapRecord:
    """One senior international appearance."""

    player_name: str
    nation: str
    cap_date: datetime
    player_fbref_id: str | None = None
    position: str | None = None


@dataclass(frozen=True)
class LeagueActivityRecord:
    """A player's start count in a specific club-league window."""

    player_name: str
    nation: str
    league: str  # soccerdata league id
    starts: int
    player_fbref_id: str | None = None
    position: str | None = None


@dataclass(frozen=True)
class SquadCandidate:
    """One heuristic squad candidate with the rule that admitted them."""

    player_name: str
    nation: str
    position: str | None
    player_fbref_id: str | None
    rationale: str


class PlayerCapsSource(Protocol):
    """Pluggable source for caps + league activity data."""

    def fetch_caps(self, nation: str, since: datetime) -> list[CapRecord]: ...

    def fetch_league_activity(self, nation: str, since: datetime) -> list[LeagueActivityRecord]: ...


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------


def _player_key(name: str, fbref_id: str | None) -> tuple[str, str | None]:
    """Stable identity for joining caps ↔ league activity.

    fbref_id is preferred when both sides report one. Falls back to name.
    """
    return (name, fbref_id)


def candidates_for(
    source: PlayerCapsSource, nation: str, as_of_date: datetime
) -> list[SquadCandidate]:
    """Apply the heuristic and return de-duplicated candidates for ``nation``.

    Returns candidates sorted by ``player_name`` for stable downstream output.
    """
    since = as_of_date - HEURISTIC_WINDOW

    caps = [c for c in source.fetch_caps(nation, since) if c.nation == nation]
    activity = [a for a in source.fetch_league_activity(nation, since) if a.nation == nation]

    caps_count: dict[tuple[str, str | None], int] = {}
    cap_meta: dict[tuple[str, str | None], CapRecord] = {}
    for cap in caps:
        if cap.cap_date < since:
            continue
        key = _player_key(cap.player_name, cap.player_fbref_id)
        caps_count[key] = caps_count.get(key, 0) + 1
        cap_meta.setdefault(key, cap)

    top5_starts: dict[tuple[str, str | None], tuple[str, int]] = {}
    activity_meta: dict[tuple[str, str | None], LeagueActivityRecord] = {}
    for record in activity:
        if record.league not in TOP5_LEAGUES or record.starts < 1:
            continue
        key = _player_key(record.player_name, record.player_fbref_id)
        existing = top5_starts.get(key)
        if existing is None or record.starts > existing[1]:
            top5_starts[key] = (record.league, record.starts)
        activity_meta.setdefault(key, record)

    candidates: dict[tuple[str, str | None], SquadCandidate] = {}
    all_keys = set(caps_count) | set(top5_starts)
    for key in all_keys:
        caps_n = caps_count.get(key, 0)
        starter = top5_starts.get(key)

        meta_cap = cap_meta.get(key)
        meta_act = activity_meta.get(key)
        name = (meta_cap or meta_act).player_name  # type: ignore[union-attr]
        position = (meta_cap.position if meta_cap else None) or (
            meta_act.position if meta_act else None
        )
        fbref_id = key[1]

        if caps_n >= 3:
            rationale = f"{caps_n} caps in trailing 12mo"
        elif starter is not None and caps_n >= 1:
            league, starts = starter
            rationale = f"{starts} start(s) in {league} in trailing 12mo + {caps_n} cap(s)"
        else:
            continue

        candidates[key] = SquadCandidate(
            player_name=name,
            nation=nation,
            position=position,
            player_fbref_id=fbref_id,
            rationale=rationale,
        )

    return sorted(candidates.values(), key=lambda c: c.player_name)

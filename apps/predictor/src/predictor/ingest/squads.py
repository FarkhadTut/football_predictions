"""WC 2026 squad ingestion.

Translates heuristic ``SquadCandidate`` outputs and announced squad JSON
files into ``players`` + ``wc_squads`` rows. ``wc_squads.source`` carries
the provenance (``heuristic`` / ``announced`` / ``merged``) so downstream
consumers can filter by view.

Idempotency
-----------
- Player natural key: ``(name, nation)``. fbref_id and position are
  populated on first insert and refreshed when a non-empty value
  appears later.
- WCSquad natural key: ``(nation, player_id, source)`` (matches the
  table-level ``UniqueConstraint``).

Announced JSON format (see ``data/wc2026_squads.json``)::

    {
      "as_of_date": "2026-06-04",
      "nations": {
        "Brazil": [
          {"name": "Vinicius Junior", "fbref_id": "...", "position": "LW"},
          ...
        ]
      }
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from predictor.db.models import Player, WCSquad
from predictor.ingest.squad_heuristic import SquadCandidate

__all__ = [
    "AnnouncedSquadEntry",
    "SquadLoadResult",
    "load_announced_squads_json",
    "write_announced_squads",
    "write_heuristic_squads",
]


@dataclass(frozen=True)
class AnnouncedSquadEntry:
    name: str
    nation: str
    fbref_id: str | None = None
    position: str | None = None


@dataclass(frozen=True)
class SquadLoadResult:
    players_added: int
    players_updated: int
    squads_added: int
    squads_skipped: int


def _upsert_player(
    session: Session,
    *,
    name: str,
    nation: str,
    fbref_id: str | None,
    position: str | None,
) -> tuple[Player, bool, bool]:
    """Insert-or-update a player by (name, nation). Returns (player, created, updated)."""
    existing = session.exec(
        select(Player).where(Player.name == name, Player.nation == nation)
    ).one_or_none()
    if existing is None:
        player = Player(name=name, nation=nation, fbref_id=fbref_id, position=position)
        session.add(player)
        session.flush()
        assert player.id is not None
        return player, True, False

    updated = False
    if fbref_id and existing.fbref_id != fbref_id:
        existing.fbref_id = fbref_id
        updated = True
    if position and existing.position != position:
        existing.position = position
        updated = True
    if updated:
        session.add(existing)
    return existing, False, updated


def _upsert_squad_row(
    session: Session,
    *,
    nation: str,
    player_id: int,
    source: str,
    as_of_date: datetime,
) -> bool:
    """Insert a WCSquad row if absent. Returns True if a new row was added."""
    existing = session.exec(
        select(WCSquad).where(
            WCSquad.nation == nation,
            WCSquad.player_id == player_id,
            WCSquad.source == source,
        )
    ).one_or_none()
    if existing is not None:
        return False
    session.add(
        WCSquad(
            nation=nation,
            player_id=player_id,
            source=source,
            as_of_date=as_of_date,
        )
    )
    return True


def write_heuristic_squads(
    session: Session,
    candidates: list[SquadCandidate],
    as_of_date: datetime,
) -> SquadLoadResult:
    """Persist heuristic candidates as ``players`` + ``wc_squads(source='heuristic')`` rows."""
    players_added = 0
    players_updated = 0
    squads_added = 0
    squads_skipped = 0

    for cand in candidates:
        player, created, updated = _upsert_player(
            session,
            name=cand.player_name,
            nation=cand.nation,
            fbref_id=cand.player_fbref_id,
            position=cand.position,
        )
        if created:
            players_added += 1
        elif updated:
            players_updated += 1
        assert player.id is not None
        if _upsert_squad_row(
            session,
            nation=cand.nation,
            player_id=player.id,
            source="heuristic",
            as_of_date=as_of_date,
        ):
            squads_added += 1
        else:
            squads_skipped += 1

    session.commit()
    return SquadLoadResult(
        players_added=players_added,
        players_updated=players_updated,
        squads_added=squads_added,
        squads_skipped=squads_skipped,
    )


def write_announced_squads(
    session: Session,
    entries: list[AnnouncedSquadEntry],
    as_of_date: datetime,
) -> SquadLoadResult:
    """Persist an announced-squad list as ``wc_squads(source='announced')`` rows."""
    players_added = 0
    players_updated = 0
    squads_added = 0
    squads_skipped = 0

    for entry in entries:
        player, created, updated = _upsert_player(
            session,
            name=entry.name,
            nation=entry.nation,
            fbref_id=entry.fbref_id,
            position=entry.position,
        )
        if created:
            players_added += 1
        elif updated:
            players_updated += 1
        assert player.id is not None
        if _upsert_squad_row(
            session,
            nation=entry.nation,
            player_id=player.id,
            source="announced",
            as_of_date=as_of_date,
        ):
            squads_added += 1
        else:
            squads_skipped += 1

    session.commit()
    return SquadLoadResult(
        players_added=players_added,
        players_updated=players_updated,
        squads_added=squads_added,
        squads_skipped=squads_skipped,
    )


def load_announced_squads_json(
    path: Path,
) -> tuple[datetime, list[AnnouncedSquadEntry]]:
    """Parse the announced-squads JSON file into typed entries + the as-of date."""
    with path.open(encoding="utf-8") as fh:
        payload: dict[str, Any] = json.load(fh)
    as_of_date = datetime.fromisoformat(payload["as_of_date"])
    entries: list[AnnouncedSquadEntry] = []
    for nation, players in payload.get("nations", {}).items():
        for p in players:
            entries.append(
                AnnouncedSquadEntry(
                    name=p["name"],
                    nation=nation,
                    fbref_id=p.get("fbref_id"),
                    position=p.get("position"),
                )
            )
    return as_of_date, entries

"""Tests for the WC squad persistence layer.

Covers both write paths (heuristic + announced) and the JSON loader. The
fixture DB is the same alembic-managed SQLite used by other ingest tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, select

from predictor.config import reset_settings_for_tests
from predictor.db.models import Player, WCSquad
from predictor.db.session import get_engine, reset_engines_for_tests
from predictor.ingest.squad_heuristic import SquadCandidate
from predictor.ingest.squads import (
    AnnouncedSquadEntry,
    SquadLoadResult,
    load_announced_squads_json,
    write_announced_squads,
    write_heuristic_squads,
)

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PREDICTOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PREDICTOR_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("PREDICTOR_DB_URL", url)
    reset_settings_for_tests()
    reset_engines_for_tests()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    yield url
    reset_engines_for_tests()
    reset_settings_for_tests()


AS_OF = datetime(2026, 6, 4)


def _candidate(
    name: str,
    *,
    nation: str = "Brazil",
    position: str | None = "FW",
    fbref_id: str | None = None,
) -> SquadCandidate:
    return SquadCandidate(
        player_name=name,
        nation=nation,
        position=position,
        player_fbref_id=fbref_id if fbref_id is not None else f"p-{name.lower()}",
        rationale="3 caps in trailing 12mo",
    )


def test_write_heuristic_inserts_players_and_squad_rows(db_url: str) -> None:
    candidates = [
        _candidate("Vinicius", position="LW"),
        _candidate("Rodrygo", position="RW"),
    ]
    with Session(get_engine()) as session:
        result = write_heuristic_squads(session, candidates, AS_OF)

    assert result == SquadLoadResult(
        players_added=2, players_updated=0, squads_added=2, squads_skipped=0
    )

    with Session(get_engine()) as session:
        players = session.exec(select(Player)).all()
        assert {p.name for p in players} == {"Vinicius", "Rodrygo"}
        assert all(p.nation == "Brazil" for p in players)

        squads = session.exec(select(WCSquad)).all()
        assert len(squads) == 2
        assert all(s.source == "heuristic" for s in squads)
        assert all(s.nation == "Brazil" for s in squads)


def test_write_heuristic_is_idempotent(db_url: str) -> None:
    candidates = [_candidate("Vinicius"), _candidate("Rodrygo")]
    with Session(get_engine()) as session:
        write_heuristic_squads(session, candidates, AS_OF)
    with Session(get_engine()) as session:
        result = write_heuristic_squads(session, candidates, AS_OF)

    assert result == SquadLoadResult(
        players_added=0, players_updated=0, squads_added=0, squads_skipped=2
    )
    with Session(get_engine()) as session:
        assert len(session.exec(select(Player)).all()) == 2
        assert len(session.exec(select(WCSquad)).all()) == 2


def test_player_upsert_refreshes_missing_metadata(db_url: str) -> None:
    """When a player was first inserted without fbref_id/position, a later
    write that carries that metadata should backfill it."""
    sparse = SquadCandidate(
        player_name="Endrick",
        nation="Brazil",
        position=None,
        player_fbref_id=None,
        rationale="3 caps in trailing 12mo",
    )
    rich = SquadCandidate(
        player_name="Endrick",
        nation="Brazil",
        position="ST",
        player_fbref_id="p-endrick",
        rationale="3 caps in trailing 12mo",
    )

    with Session(get_engine()) as session:
        first = write_heuristic_squads(session, [sparse], AS_OF)
    with Session(get_engine()) as session:
        second = write_heuristic_squads(session, [rich], AS_OF)

    assert first.players_added == 1
    assert second == SquadLoadResult(
        players_added=0, players_updated=1, squads_added=0, squads_skipped=1
    )
    with Session(get_engine()) as session:
        player = session.exec(select(Player).where(Player.name == "Endrick")).one()
        assert player.fbref_id == "p-endrick"
        assert player.position == "ST"


def test_announced_and_heuristic_coexist_for_same_player(db_url: str) -> None:
    """Source provenance: the same player can have both a heuristic and an
    announced row — they differ on the ``source`` column."""
    cand = _candidate("Vinicius", position="LW")
    with Session(get_engine()) as session:
        write_heuristic_squads(session, [cand], AS_OF)
    announced = AnnouncedSquadEntry(
        name="Vinicius", nation="Brazil", fbref_id="p-vinicius", position="LW"
    )
    with Session(get_engine()) as session:
        result = write_announced_squads(session, [announced], AS_OF)

    assert result == SquadLoadResult(
        players_added=0, players_updated=0, squads_added=1, squads_skipped=0
    )
    with Session(get_engine()) as session:
        squads = session.exec(select(WCSquad)).all()
        assert {s.source for s in squads} == {"heuristic", "announced"}
        assert len({s.player_id for s in squads}) == 1  # same player row


def test_announced_write_is_idempotent(db_url: str) -> None:
    entries = [
        AnnouncedSquadEntry(name="Mbappe", nation="France", fbref_id="p-mbappe", position="ST"),
    ]
    with Session(get_engine()) as session:
        write_announced_squads(session, entries, AS_OF)
    with Session(get_engine()) as session:
        result = write_announced_squads(session, entries, AS_OF)
    assert result == SquadLoadResult(
        players_added=0, players_updated=0, squads_added=0, squads_skipped=1
    )


def test_load_announced_squads_json_round_trip(tmp_path: Path) -> None:
    payload = {
        "as_of_date": "2026-06-04",
        "nations": {
            "Brazil": [
                {"name": "Vinicius Junior", "fbref_id": "p-vini", "position": "LW"},
                {"name": "Rodrygo", "fbref_id": "p-rodrygo", "position": "RW"},
            ],
            "France": [
                {"name": "Mbappe", "fbref_id": None, "position": "ST"},
            ],
        },
    }
    path = tmp_path / "squads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    as_of, entries = load_announced_squads_json(path)
    assert as_of == datetime(2026, 6, 4)
    assert len(entries) == 3
    by_name = {e.name: e for e in entries}
    assert by_name["Vinicius Junior"].nation == "Brazil"
    assert by_name["Vinicius Junior"].fbref_id == "p-vini"
    assert by_name["Mbappe"].nation == "France"
    assert by_name["Mbappe"].fbref_id is None


def test_load_announced_squads_json_tolerates_missing_optional_fields(tmp_path: Path) -> None:
    payload = {
        "as_of_date": "2026-06-04",
        "nations": {"Argentina": [{"name": "Messi"}]},
    }
    path = tmp_path / "squads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    _, entries = load_announced_squads_json(path)
    assert len(entries) == 1
    assert entries[0] == AnnouncedSquadEntry(
        name="Messi", nation="Argentina", fbref_id=None, position=None
    )


def test_load_announced_squads_json_empty_nations(tmp_path: Path) -> None:
    """Phase 0 ships with an empty scaffold — loader must handle it."""
    payload = {"as_of_date": "2026-06-04", "nations": {}}
    path = tmp_path / "squads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    as_of, entries = load_announced_squads_json(path)
    assert as_of == datetime(2026, 6, 4)
    assert entries == []

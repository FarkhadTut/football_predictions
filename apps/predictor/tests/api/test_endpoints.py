"""HTTP endpoint tests for the Phase 0 FastAPI backend (REQ-008, REQ-009).

Covers:
* TEST-008 — ``GET /fixtures`` returns scheduled matches sorted by kickoff
* TEST-009 — ``GET /matches/{id}/notes`` returns parsed Claude note or 404
* TEST-010 — ``GET /matches/{id}/notes`` returns 422 for malformed note
* REQ-008 contract — ``POST /matches/{id}/predict`` returns 200 (cached) /
  202 (enqueued fit) with ``model_run_id``.

Each test isolates state via:
* ``PREDICTOR_DB_URL`` pointed at a per-test on-disk SQLite file
* ``PREDICTOR_NOTES_DIR`` pointed at a per-test temp directory
* ``alembic upgrade head`` to build the schema
* :func:`reset_settings_for_tests` + :func:`reset_engines_for_tests`
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlmodel import Session

from predictor.config import reset_settings_for_tests
from predictor.db.models import (
    Match,
    ModelRun,
    Prediction,
    Team,
)
from predictor.db.session import get_engine, reset_engines_for_tests

PREDICTOR_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PREDICTOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PREDICTOR_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db_url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    notes_dir = tmp_path / "claude_notes"
    notes_dir.mkdir()
    monkeypatch.setenv("PREDICTOR_DB_URL", db_url)
    monkeypatch.setenv("PREDICTOR_NOTES_DIR", str(notes_dir))
    reset_settings_for_tests()
    reset_engines_for_tests()
    command.upgrade(_alembic_config(db_url), "head")

    # Import inside the fixture so the app picks up the patched settings.
    from predictor.api.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    reset_engines_for_tests()
    reset_settings_for_tests()


def _seed_two_teams(session: Session) -> tuple[Team, Team]:
    home = Team(name="Brazil", country="BR")
    away = Team(name="Argentina", country="AR")
    session.add(home)
    session.add(away)
    session.commit()
    session.refresh(home)
    session.refresh(away)
    assert home.id is not None and away.id is not None
    return home, away


def _seed_match(
    session: Session,
    *,
    home_id: int,
    away_id: int,
    kickoff: datetime,
    season: str = "2026",
    status: str = "scheduled",
) -> Match:
    m = Match(
        competition="WC",
        season=season,
        home_team_id=home_id,
        away_team_id=away_id,
        kickoff_utc=kickoff,
        status=status,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    assert m.id is not None
    return m


# ---------------------------------------------------------------------------
# TEST-008 — GET /fixtures
# ---------------------------------------------------------------------------


def test_get_fixtures_returns_scheduled_matches_sorted_by_kickoff(
    client: TestClient,
) -> None:
    base = datetime(2026, 6, 11, 18, 0, tzinfo=UTC).replace(tzinfo=None)
    with Session(get_engine()) as session:
        home, away = _seed_two_teams(session)
        assert home.id is not None and away.id is not None
        # Seed three fixtures in deliberately scrambled chronological order.
        _seed_match(session, home_id=home.id, away_id=away.id, kickoff=base + timedelta(days=2))
        _seed_match(session, home_id=away.id, away_id=home.id, kickoff=base)
        _seed_match(session, home_id=home.id, away_id=away.id, kickoff=base + timedelta(days=1))

    resp = client.get("/fixtures")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 3
    kickoffs = [f["kickoff_utc"] for f in body]
    assert kickoffs == sorted(kickoffs), "fixtures must be in ascending kickoff order"
    # Required Fixture fields.
    for f in body:
        assert {"id", "home_team", "away_team", "kickoff_utc", "competition"} <= set(f)


# ---------------------------------------------------------------------------
# TEST-009 / TEST-010 — GET /matches/{id}/notes
# ---------------------------------------------------------------------------


def _valid_note_payload(match_id: int) -> dict[str, object]:
    return {
        "match_id": match_id,
        "created_at": "2026-06-11T12:00:00+00:00",
        "summary": "Brazil rested key players in the friendly.",
        "qualitative_deltas": [
            {"market": "1x2", "log_odds_shift": 0.15},
            {"market": "ou_2_5", "log_odds_shift": -0.1},
        ],
        "confidence": 0.7,
        "sources": ["https://example.com/brazil-friendly"],
    }


def test_get_match_notes_returns_parsed_note_when_file_exists(
    client: TestClient, tmp_path: Path
) -> None:
    notes_dir = Path(client.app.state.settings.notes_dir)  # type: ignore[attr-defined]
    payload = _valid_note_payload(42)
    (notes_dir / "match-42.json").write_text(json.dumps(payload), encoding="utf-8")

    resp = client.get("/matches/42/notes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["match_id"] == 42
    assert body["confidence"] == 0.7
    assert len(body["qualitative_deltas"]) == 2
    assert {d["market"] for d in body["qualitative_deltas"]} == {"1x2", "ou_2_5"}


def test_get_match_notes_returns_404_when_file_missing(client: TestClient) -> None:
    resp = client.get("/matches/43/notes")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no_note"}


def test_get_match_notes_returns_422_when_payload_invalid(client: TestClient) -> None:
    notes_dir = Path(client.app.state.settings.notes_dir)  # type: ignore[attr-defined]
    # missing required `confidence`
    bad = _valid_note_payload(44)
    bad.pop("confidence")
    (notes_dir / "match-44.json").write_text(json.dumps(bad), encoding="utf-8")

    resp = client.get("/matches/44/notes")
    assert resp.status_code == 422
    body = resp.json()
    # FastAPI / Pydantic style error envelope
    assert "detail" in body
    # At least one error mentions the missing 'confidence' field
    flat = json.dumps(body["detail"])
    assert "confidence" in flat


# ---------------------------------------------------------------------------
# REQ-008 contract — POST /matches/{id}/predict
# ---------------------------------------------------------------------------


def test_post_predict_returns_202_when_no_cached_prediction(
    client: TestClient,
) -> None:
    with Session(get_engine()) as session:
        home, away = _seed_two_teams(session)
        assert home.id is not None and away.id is not None
        match = _seed_match(
            session,
            home_id=home.id,
            away_id=away.id,
            kickoff=datetime(2026, 6, 11, 18, 0),
        )
        match_id = match.id
    assert match_id is not None

    resp = client.post(f"/matches/{match_id}/predict", json={"force_refit": False})
    assert resp.status_code == 202
    body = resp.json()
    assert isinstance(body["model_run_id"], int)
    assert body["status"] == "running"


def test_post_predict_returns_200_when_cached_prediction_exists(
    client: TestClient,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(get_engine()) as session:
        home, away = _seed_two_teams(session)
        assert home.id is not None and away.id is not None
        match = _seed_match(
            session,
            home_id=home.id,
            away_id=away.id,
            kickoff=datetime(2026, 6, 11, 18, 0),
        )
        run = ModelRun(
            model_version="dc-v0",
            git_sha="deadbeef",
            training_cutoff_utc=now,
            fitter_config_json={},
            created_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None and match.id is not None
        # Seed a full 1x2 marginal so the response is observable.
        for outcome, p in (("home", 0.5), ("draw", 0.3), ("away", 0.2)):
            session.add(
                Prediction(
                    match_id=match.id,
                    market="1x2",
                    outcome=outcome,
                    probability=p,
                    model_run_id=run.id,
                    computed_at=now,
                )
            )
        session.commit()
        match_id = match.id

    resp = client.post(
        f"/matches/{match_id}/predict",
        json={"model_version": "dc-v0", "force_refit": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is True
    assert body["model_version"] == "dc-v0"
    # 1x2 marginals echoed back
    assert set(body["markets"]["1x2"]) == {"home", "draw", "away"}
    assert body["markets"]["1x2"]["home"] == pytest.approx(0.5)


def test_post_predict_force_refit_enqueues_even_when_cached(
    client: TestClient,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(get_engine()) as session:
        home, away = _seed_two_teams(session)
        assert home.id is not None and away.id is not None
        match = _seed_match(
            session,
            home_id=home.id,
            away_id=away.id,
            kickoff=datetime(2026, 6, 11, 18, 0),
        )
        run = ModelRun(
            model_version="dc-v0",
            git_sha="deadbeef",
            training_cutoff_utc=now,
            fitter_config_json={},
            created_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None and match.id is not None
        session.add(
            Prediction(
                match_id=match.id,
                market="1x2",
                outcome="home",
                probability=0.5,
                model_run_id=run.id,
                computed_at=now,
            )
        )
        session.commit()
        match_id = match.id

    resp = client.post(
        f"/matches/{match_id}/predict",
        json={"model_version": "dc-v0", "force_refit": True},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"


def test_post_predict_returns_404_for_unknown_match(client: TestClient) -> None:
    resp = client.post("/matches/9999/predict", json={"force_refit": False})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /matches/{id} — minimal detail endpoint
# ---------------------------------------------------------------------------


def test_get_match_returns_match_detail(client: TestClient) -> None:
    with Session(get_engine()) as session:
        home, away = _seed_two_teams(session)
        assert home.id is not None and away.id is not None
        match = _seed_match(
            session,
            home_id=home.id,
            away_id=away.id,
            kickoff=datetime(2026, 6, 11, 18, 0),
        )
        match_id = match.id
    assert match_id is not None

    resp = client.get(f"/matches/{match_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == match_id
    assert body["home_team"] == "Brazil"
    assert body["away_team"] == "Argentina"
    assert body["status"] == "scheduled"


def test_get_match_returns_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get("/matches/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CORS — Vite dev origin must be allowed (browser at :5173 → API at :8000)
# ---------------------------------------------------------------------------


def test_cors_allows_vite_dev_origin(client: TestClient) -> None:
    resp = client.get("/fixtures", headers={"Origin": "http://127.0.0.1:5173"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    resp = client.get("/fixtures", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200
    # Starlette's CORSMiddleware omits the header for disallowed origins, which
    # is exactly what tells the browser to block the response.
    assert "access-control-allow-origin" not in resp.headers

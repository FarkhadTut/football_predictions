"""``POST /matches/{id}/predict`` — REQ-008 contract.

Phase 0 implements the *contract* — the cached path returns existing
predictions verbatim, the enqueued path records a ``model_runs`` row marked
``running`` and returns ``202``. Wiring the actual background fit (sub-step
7.2 / Step 8) is out of scope for the contract test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from predictor.api.schemas import (
    PredictCachedResponse,
    PredictEnqueuedResponse,
    PredictRequest,
)
from predictor.db.models import Match, ModelRun, Prediction
from predictor.db.session import get_engine

router = APIRouter()

DEFAULT_MODEL_VERSION = "dc-v0"


def _latest_cached(
    session: Session, *, match_id: int, model_version: str
) -> tuple[ModelRun, list[Prediction]] | None:
    """Return the most recent ``(ModelRun, predictions[])`` for the (match,
    version) tuple, or ``None`` if none exists."""
    runs = session.exec(
        select(ModelRun)
        .where(ModelRun.model_version == model_version)
        .order_by(ModelRun.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    for run in runs:
        assert run.id is not None
        preds = session.exec(
            select(Prediction).where(
                Prediction.match_id == match_id,
                Prediction.model_run_id == run.id,
            )
        ).all()
        if preds:
            return run, list(preds)
    return None


def _markets_payload(preds: list[Prediction]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for p in preds:
        out.setdefault(p.market, {})[p.outcome] = float(p.probability)
    return out


@router.post(
    "/matches/{match_id}/predict",
    responses={
        200: {"model": PredictCachedResponse},
        202: {"model": PredictEnqueuedResponse},
    },
)
def predict(match_id: int, body: PredictRequest) -> Any:
    model_version = body.model_version or DEFAULT_MODEL_VERSION
    with Session(get_engine()) as session:
        match = session.get(Match, match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="match_not_found")

        if not body.force_refit:
            cached = _latest_cached(session, match_id=match_id, model_version=model_version)
            if cached is not None:
                run, preds = cached
                assert run.id is not None
                return PredictCachedResponse(
                    match_id=match_id,
                    model_version=model_version,
                    model_run_id=run.id,
                    markets=_markets_payload(preds),
                )

        # Enqueue a fit — Phase 0 records the run row synchronously; the
        # background worker that consumes "running" rows lands in Phase 1.
        now = datetime.now(UTC).replace(tzinfo=None)
        run = ModelRun(
            model_version=model_version,
            git_sha="pending",
            training_cutoff_utc=now,
            fitter_config_json={"trigger": "predict_endpoint", "force_refit": body.force_refit},
            created_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        return JSONResponse(
            status_code=202,
            content=PredictEnqueuedResponse(model_run_id=run.id).model_dump(),
        )

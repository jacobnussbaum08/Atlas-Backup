"""Machine-learning API (Phase 5A).

Reads: an overview, dataset versions, training-run history and details (with walk-forward,
segment and calibration evidence), candidate comparisons, the model registry with each
version's status history, and prediction history (with an exact replay check).
Write (operator token, audited): a model lifecycle transition. Transitions are explicit
operator decisions; the API never promotes a model on its own.

There is no endpoint that trains on request (training is a CLI job), and none that
submits, sizes or approves anything: predictions are analytical records.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.market_data import market_data, require_operator
from app.core.exceptions import (
    DependencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
)
from app.database.sessions import session_scope
from app.ml_engine import queries
from app.ml_engine.enums import (
    LifecycleStatus,
    ModelType,
    PredictionValidity,
    RunStatus,
)
from app.ml_engine.inference.engine import InferenceError
from app.ml_engine.registry import lifecycle
from app.ml_engine.registry.artifacts import ArtifactError
from app.ml_engine.runtime import MLContext

router = APIRouter(prefix="/ml", tags=["ml"])
_lock = threading.Lock()


def ml(request: Request) -> MLContext:
    """The ML context, built on first use."""
    state = request.app.state
    context: MLContext | None = getattr(state, "ml", None)
    if context is None:
        factory = getattr(state, "ml_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the ML engine is not configured")
        with _lock:
            context = getattr(state, "ml", None)
            if context is None:
                market = market_data(request)
                context = factory(market.provider.provider_id, market.provider.feed)
                state.ml = context
    return context


Context = Annotated[MLContext, Depends(ml)]
Limit = Annotated[int, Query(ge=1, le=200)]


class Overview(BaseModel):
    datasets: int
    dataset_versions: dict[str, int]
    training_runs: dict[str, int]
    models: dict[str, int]
    predictions: int
    segments: list[str]
    splits: list[str]
    notes: list[str]


class TransitionBody(BaseModel):
    status: LifecycleStatus
    reason: str = Field(min_length=3, max_length=2000)


class TransitionResult(BaseModel):
    model: queries.ModelDetail
    previous_status: str


class CompareResponse(BaseModel):
    runs: list[dict[str, Any]]
    comparable: bool
    missing: list[str]
    notes: list[str]


@router.get("/overview", response_model=Overview)
def overview(ctx: Context) -> Overview:
    with session_scope(ctx.session_factory) as session:
        return Overview(**queries.overview(session))


@router.get("/datasets", response_model=list[queries.DatasetVersionSummary])
def list_datasets(ctx: Context, limit: Limit = 100) -> list[queries.DatasetVersionSummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.dataset_versions(session, limit)


@router.get("/datasets/{version_id}", response_model=queries.DatasetVersionDetail)
def get_dataset(ctx: Context, version_id: uuid.UUID) -> queries.DatasetVersionDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.dataset_version(session, version_id)
    if found is None:
        raise NotFoundError(f"dataset version {version_id} does not exist")
    return found


@router.get("/runs", response_model=list[queries.RunSummary])
def list_runs(
    ctx: Context,
    status: RunStatus | None = None,
    model_type: ModelType | None = None,
    dataset_version_id: uuid.UUID | None = None,
    limit: Limit = 50,
) -> list[queries.RunSummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.runs(
            session,
            status=None if status is None else status.value,
            model_type=None if model_type is None else model_type.value,
            dataset_version_id=dataset_version_id,
            limit=limit,
        )


@router.get("/runs/{run_id}", response_model=queries.RunDetail)
def get_run(ctx: Context, run_id: uuid.UUID) -> queries.RunDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.run(session, run_id)
    if found is None:
        raise NotFoundError(f"training run {run_id} does not exist")
    return found


@router.get("/compare", response_model=CompareResponse)
def compare(
    ctx: Context, run_ids: Annotated[list[uuid.UUID], Query(min_length=1, max_length=10)]
) -> CompareResponse:
    with session_scope(ctx.session_factory) as session:
        return CompareResponse(**queries.compare(session, run_ids))


@router.get("/models", response_model=list[queries.ModelSummary])
def list_models(
    ctx: Context, status: LifecycleStatus | None = None, limit: Limit = 100
) -> list[queries.ModelSummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.models(session, status=None if status is None else status.value, limit=limit)


@router.get("/models/{model_id}", response_model=queries.ModelDetail)
def get_model(ctx: Context, model_id: uuid.UUID) -> queries.ModelDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.model(session, model_id)
    if found is None:
        raise NotFoundError(f"model version {model_id} does not exist")
    return found


@router.post(
    "/models/{model_id}/status",
    response_model=TransitionResult,
    dependencies=[Depends(require_operator)],
)
def set_model_status(ctx: Context, model_id: uuid.UUID, body: TransitionBody) -> TransitionResult:
    """Explicit lifecycle transition (operator token, audited). Disallowed transitions,
    missing out-of-sample evidence and a second APPROVED version are refused (409)."""
    try:
        with session_scope(ctx.session_factory) as session:
            result = lifecycle.transition(
                session, model_id, body.status, reason=body.reason, actor="operator:api"
            )
            previous = result.previous.value
    except lifecycle.ModelNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    with session_scope(ctx.session_factory) as session:
        detail = queries.model(session, model_id)
    if detail is None:  # pragma: no cover - it was just transitioned
        raise NotFoundError(f"model version {model_id} does not exist")
    return TransitionResult(model=detail, previous_status=previous)


@router.get("/predictions", response_model=list[queries.PredictionView])
def list_predictions(
    ctx: Context,
    model_version_id: uuid.UUID | None = None,
    asset_id: uuid.UUID | None = None,
    validity: PredictionValidity | None = None,
    limit: Limit = 50,
) -> list[queries.PredictionView]:
    with session_scope(ctx.session_factory) as session:
        return queries.predictions(
            session,
            model_version_id=model_version_id,
            asset_id=asset_id,
            validity=None if validity is None else validity.value,
            limit=limit,
        )


@router.get("/predictions/{prediction_id}", response_model=queries.PredictionDetail)
def get_prediction(ctx: Context, prediction_id: uuid.UUID) -> queries.PredictionDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.prediction(session, prediction_id)
    if found is None:
        raise NotFoundError(f"prediction {prediction_id} does not exist")
    return found


@router.get("/predictions/{prediction_id}/replay")
def replay_prediction(ctx: Context, prediction_id: uuid.UUID) -> dict[str, Any]:
    """Recompute a stored prediction from its stored inputs and verified artifact."""
    try:
        return ctx.inference.replay(prediction_id)
    except InferenceError as exc:
        raise NotFoundError(str(exc)) from exc
    except ArtifactError as exc:
        raise InvalidRequestError(str(exc)) from exc


__all__ = ["ml", "router"]

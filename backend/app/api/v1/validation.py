"""Strategy research and validation API (Phase 8B). **Simulations over stored data only.**

Reads (GET): validation runs with their walk-forward folds, trials, robustness results and
segments; strategy comparisons; parameter sensitivity; ML model comparisons; champions,
challengers and promotion history; degradation monitoring.

Operator-gated writes (POST, ``X-Atlas-Operator-Token``):

* create a validation run (queued on the API process's validation thread), reproduce one,
  evaluate a completed run's untouched holdout (numbered, once per request);
* store an ML model comparison;
* champion/challenger: create a candidate, move it through its lifecycle (every change an
  explicit decision with a reason), record a shadow evaluation, adopt an incumbent;
* run a monitoring pass; approve a monitoring rule.

Nothing here submits an order, changes a risk limit, or promotes anything by itself (spec
24.1, 19.2): approval and rollback are explicit operator requests that pass the evidence
gates in :mod:`app.learning.governance`.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.market_data import require_operator
from app.core.exceptions import DependencyUnavailableError, InvalidRequestError
from app.database.sessions import session_scope
from app.learning import models as model_eval
from app.learning import queries
from app.learning.enums import RobustnessMethod, RuleAction, RunStatus, SubjectKind
from app.learning.monitoring import MonitorRule
from app.learning.runtime import ValidationContext
from app.learning.walkforward import ExperimentConfig

router = APIRouter(prefix="/validation", tags=["validation"])
_lock = threading.Lock()
ACTOR = "operator:api"


def validation(request: Request) -> ValidationContext:
    state = request.app.state
    context: ValidationContext | None = getattr(state, "validation", None)
    if context is None:
        factory = getattr(state, "validation_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the validation laboratory is not configured here")
        with _lock:
            context = getattr(state, "validation", None)
            if context is None:
                context = factory()
                state.validation = context
    return context


Context = Annotated[ValidationContext, Depends(validation)]
Operator = [Depends(require_operator)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateBody(_Body):
    subject_kind: SubjectKind
    strategy_id: str | None = None
    parameters: dict[str, Any] | None = None
    source_run_id: uuid.UUID | None = None
    model_version_id: uuid.UUID | None = None
    reason: str = Field(min_length=1, max_length=2000)


class TransitionBody(_Body):
    action: Literal["start_validation", "shadow", "approve", "reject", "retire", "rollback"]
    reason: str = Field(min_length=1, max_length=2000)
    run_id: uuid.UUID | None = None
    replaces: uuid.UUID | None = None


class AdoptBody(_Body):
    strategy_id: str
    reason: str = Field(min_length=1, max_length=2000)


class RuleBody(_Body):
    subject_kind: SubjectKind
    subject_key: str = Field(min_length=1, max_length=128)
    rule: MonitorRule = Field(default_factory=MonitorRule)
    action: RuleAction = RuleAction.WARN
    active: bool = True
    reason: str = Field(min_length=1, max_length=2000)


class ModelComparisonBody(_Body):
    dataset_version_id: uuid.UUID | None = None


# ---------------------------------------------------------------------- overview
@router.get("/overview")
def overview(ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.overview(session, ctx.clock)


@router.get("/options")
def options(ctx: Context) -> dict[str, Any]:
    return queries.options(ctx.registry)


# ---------------------------------------------------------------------- runs
@router.get("/runs")
def list_runs(
    ctx: Context,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    strategy: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.runs(session, limit=limit, status=status_filter, strategy=strategy)


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED, dependencies=Operator)
def create_run(body: ExperimentConfig, ctx: Context) -> dict[str, Any]:
    """Store a validation run (configuration, pinned versions, reserved holdout) and run it
    on the validation thread."""
    run_id = ctx.service.create(body, actor=ACTOR)
    ctx.submit(run_id)
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, run_id)


@router.get("/compare")
def compare(ctx: Context, run_ids: Annotated[str, Query()]) -> dict[str, Any]:
    try:
        ids = [uuid.UUID(x) for x in run_ids.split(",") if x.strip()]
    except ValueError:
        raise InvalidRequestError("run_ids must be comma-separated UUIDs") from None
    if not 1 <= len(ids) <= 10:
        raise InvalidRequestError("compare between 1 and 10 runs")
    with session_scope(ctx.session_factory) as session:
        return queries.compare_runs(session, ids)


@router.get("/runs/{run_id}")
def run_detail(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, run_id)


@router.post(
    "/runs/{run_id}/reproduce", status_code=status.HTTP_202_ACCEPTED, dependencies=Operator
)
def reproduce(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    new_id = ctx.service.prepare_reproduction(run_id, actor=ACTOR)
    ctx.submit(new_id)
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, new_id)


@router.post("/runs/{run_id}/holdout", dependencies=Operator)
def evaluate_holdout(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    """Replay the run's frozen final parameters on its untouched final period (numbered)."""
    holdout_id = ctx.service.evaluate_holdout(run_id, actor=ACTOR)
    with session_scope(ctx.session_factory) as session:
        from app.database.models.learning import ValidationHoldoutRecord

        row = session.get(ValidationHoldoutRecord, holdout_id)
        assert row is not None  # noqa: S101
        return queries.holdout_row(row)


@router.get("/runs/{run_id}/folds")
def folds(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.folds(session, run_id)


@router.get("/runs/{run_id}/trials")
def trials(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.trials(session, run_id)


@router.get("/runs/{run_id}/robustness")
def robustness(
    run_id: uuid.UUID, ctx: Context, method: RobustnessMethod | None = None
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.robustness(session, run_id, method)


@router.get("/runs/{run_id}/sensitivity")
def sensitivity(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.robustness(session, run_id, RobustnessMethod.PARAMETER_SENSITIVITY)


@router.get("/runs/{run_id}/segments")
def segments(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.segments(session, run_id)


# ---------------------------------------------------------------------- models
@router.get("/models/compare")
def model_comparison(ctx: Context) -> list[dict[str, Any]]:
    """Live comparison of stored 5A training runs (predictive vs financial, baselines)."""
    with session_scope(ctx.session_factory) as session:
        return queries.model_comparison(session)


@router.post("/models/comparisons", status_code=status.HTTP_201_CREATED, dependencies=Operator)
def store_model_comparison(body: ModelComparisonBody, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        cid = model_eval.store_comparison(
            session, actor=ACTOR, dataset_version_id=body.dataset_version_id
        )
        return {"id": str(cid)}


@router.get("/comparisons")
def comparisons(ctx: Context, kind: SubjectKind | None = None) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.comparisons(session, kind)


# ---------------------------------------------------------------------- governance
@router.get("/champions")
def champions(ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.champions(session, ctx.registry)


@router.post("/champions/adopt", status_code=status.HTTP_201_CREATED, dependencies=Operator)
def adopt(body: AdoptBody, ctx: Context) -> dict[str, Any]:
    cid = ctx.governance.adopt_incumbent(body.strategy_id, actor=ACTOR, reason=body.reason)
    with session_scope(ctx.session_factory) as session:
        return queries.candidate_detail(session, cid)


@router.get("/candidates")
def candidates(ctx: Context, kind: SubjectKind | None = None) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.candidates(session, kind)


@router.post("/candidates", status_code=status.HTTP_201_CREATED, dependencies=Operator)
def create_candidate(body: CandidateBody, ctx: Context) -> dict[str, Any]:
    if body.subject_kind is SubjectKind.STRATEGY:
        if body.strategy_id is None:
            raise InvalidRequestError("a strategy candidate needs strategy_id")
        cid = ctx.governance.create_strategy_candidate(
            body.strategy_id,
            body.parameters or {},
            actor=ACTOR,
            reason=body.reason,
            source_run_id=body.source_run_id,
        )
    else:
        if body.model_version_id is None:
            raise InvalidRequestError("a model candidate needs model_version_id")
        cid = ctx.governance.create_model_candidate(
            body.model_version_id, actor=ACTOR, reason=body.reason
        )
    with session_scope(ctx.session_factory) as session:
        return queries.candidate_detail(session, cid)


@router.get("/candidates/{candidate_id}")
def candidate_detail(candidate_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.candidate_detail(session, candidate_id)


@router.post("/candidates/{candidate_id}/transition", dependencies=Operator)
def transition(candidate_id: uuid.UUID, body: TransitionBody, ctx: Context) -> dict[str, Any]:
    g = ctx.governance
    if body.action == "start_validation":
        g.start_validation(candidate_id, actor=ACTOR, reason=body.reason)
    elif body.action == "shadow":
        g.to_shadow(candidate_id, actor=ACTOR, reason=body.reason, run_id=body.run_id)
    elif body.action == "approve":
        g.approve(candidate_id, actor=ACTOR, reason=body.reason, replaces=body.replaces)
    elif body.action == "reject":
        g.reject(candidate_id, actor=ACTOR, reason=body.reason)
    elif body.action == "retire":
        g.retire(candidate_id, actor=ACTOR, reason=body.reason)
    else:
        g.rollback(candidate_id, actor=ACTOR, reason=body.reason)
    with session_scope(ctx.session_factory) as session:
        return queries.candidate_detail(session, candidate_id)


@router.post("/candidates/{candidate_id}/shadow-evaluation", dependencies=Operator)
def shadow_evaluation(candidate_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    ctx.governance.evaluate_shadow(candidate_id, actor=ACTOR)
    with session_scope(ctx.session_factory) as session:
        return queries.candidate_detail(session, candidate_id)


@router.get("/promotions")
def promotions(ctx: Context) -> list[dict[str, Any]]:
    """Every approval, rejection, retirement and rollback with its evidence."""
    with session_scope(ctx.session_factory) as session:
        return queries.approvals(session)


# ---------------------------------------------------------------------- monitoring
@router.get("/monitoring")
def monitoring(ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.monitoring(session)


@router.get("/alerts")
def alerts(ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.alerts(session)


@router.post("/monitoring/run", dependencies=Operator)
def run_monitoring(ctx: Context) -> dict[str, Any]:
    _monitor_id, report = ctx.monitoring.run()
    return report


@router.post("/monitoring/rules", status_code=status.HTTP_201_CREATED, dependencies=Operator)
def approve_rule(body: RuleBody, ctx: Context) -> dict[str, Any]:
    rid = ctx.monitoring.approve_rule(
        body.subject_kind,
        body.subject_key,
        body.rule,
        body.action,
        actor=ACTOR,
        reason=body.reason,
        active=body.active,
    )
    return {"id": str(rid)}

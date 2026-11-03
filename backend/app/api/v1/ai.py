"""Central AI decision engine API (Phase 5B). Read-only.

Decision history and details (proposals considered, scoring components, evidence, model
contributions, conflicts), proposal rankings per run, a replay check of a stored run,
the decision policies, LLM analyses and requests, provider usage and estimated cost, and
baseline comparisons.

Runs, LLM analysis and evaluations are CLI/worker jobs. No endpoint here starts a run,
calls an LLM, approves, sizes or submits anything: a prioritized decision is a candidate for
the future risk engine, not an approved trade.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.ai_engine import queries
from app.ai_engine.coordinator.service import CoordinatorError
from app.ai_engine.enums import DecisionOutcome, DecisionRunStatus, LLMRequestStatus, LLMTask
from app.ai_engine.runtime import AIContext
from app.core.exceptions import DependencyUnavailableError, NotFoundError
from app.database.sessions import session_scope

router = APIRouter(prefix="/ai", tags=["ai"])
_lock = threading.Lock()


def ai(request: Request) -> AIContext:
    """The AI context, built on first use."""
    state = request.app.state
    context: AIContext | None = getattr(state, "ai", None)
    if context is None:
        factory = getattr(state, "ai_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the AI decision engine is not configured")
        with _lock:
            context = getattr(state, "ai", None)
            if context is None:
                context = factory()
                state.ai = context
    return context


Context = Annotated[AIContext, Depends(ai)]
Limit = Annotated[int, Query(ge=1, le=200)]


class Overview(BaseModel):
    runs: dict[str, int]
    decisions: dict[str, int]
    llm_analyses: dict[str, int]
    latest_run: dict[str, Any] | None
    llm: dict[str, Any]
    current_policy_sha256: str
    disclaimer: str
    llm_note: str


class Rankings(BaseModel):
    run: queries.RunSummary | None
    decisions: list[queries.DecisionSummary]
    disclaimer: str


@router.get("/overview", response_model=Overview)
def overview(ctx: Context) -> Overview:
    with session_scope(ctx.session_factory) as session:
        data = queries.overview(session, ctx.settings)
    return Overview(**data, current_policy_sha256=ctx.policy.sha256())


@router.get("/policies", response_model=list[queries.PolicyVersionView])
def list_policies(ctx: Context) -> list[queries.PolicyVersionView]:
    with session_scope(ctx.session_factory) as session:
        return queries.policies(session)


@router.get("/runs", response_model=list[queries.RunSummary])
def list_runs(
    ctx: Context,
    status: DecisionRunStatus | None = None,
    outcome: str | None = Query(None, pattern="^(OPPORTUNITIES_PRIORITIZED|NO_OPPORTUNITY)$"),
    before: datetime | None = None,
    limit: Limit = 50,
) -> list[queries.RunSummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.runs(
            session,
            status=None if status is None else status.value,
            outcome=outcome,
            before=before,
            limit=limit,
        )


@router.get("/runs/{run_id}", response_model=queries.RunDetail)
def get_run(ctx: Context, run_id: uuid.UUID) -> queries.RunDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.run(session, run_id)
    if found is None:
        raise NotFoundError(f"decision run {run_id} does not exist")
    return found


@router.get("/runs/{run_id}/replay")
def replay_run(ctx: Context, run_id: uuid.UUID) -> dict[str, Any]:
    """Re-evaluate a stored run from its stored inputs (no provider call, no fresh read)."""
    try:
        return ctx.coordinator.replay(run_id)
    except CoordinatorError as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/rankings", response_model=Rankings)
def get_rankings(ctx: Context, run_id: uuid.UUID | None = None) -> Rankings:
    with session_scope(ctx.session_factory) as session:
        found, ranked = queries.rankings(session, run_id)
    if run_id is not None and found is None:
        raise NotFoundError(f"completed decision run {run_id} does not exist")
    return Rankings(run=found, decisions=ranked, disclaimer=queries.DISCLAIMER)


@router.get("/decisions", response_model=list[queries.DecisionSummary])
def list_decisions(
    ctx: Context,
    outcome: DecisionOutcome | None = None,
    asset_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    before: datetime | None = None,
    limit: Limit = 50,
) -> list[queries.DecisionSummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.decisions(
            session,
            outcome=None if outcome is None else outcome.value,
            asset_id=asset_id,
            run_id=run_id,
            before=before,
            limit=limit,
        )


@router.get("/decisions/{decision_id}", response_model=queries.DecisionDetail)
def get_decision(ctx: Context, decision_id: uuid.UUID) -> queries.DecisionDetail:
    with session_scope(ctx.session_factory) as session:
        found = queries.decision(session, decision_id)
    if found is None:
        raise NotFoundError(f"decision {decision_id} does not exist")
    return found


@router.get("/decisions/{decision_id}/evidence", response_model=list[queries.EvidenceView])
def get_evidence(ctx: Context, decision_id: uuid.UUID) -> list[queries.EvidenceView]:
    with session_scope(ctx.session_factory) as session:
        found = queries.evidence(session, decision_id)
    if found is None:
        raise NotFoundError(f"decision {decision_id} does not exist")
    return found


@router.get("/decisions/{decision_id}/models")
def get_model_contributions(ctx: Context, decision_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        found = queries.model_contributions(session, decision_id)
    if found is None:
        raise NotFoundError(f"decision {decision_id} does not exist")
    return found


@router.get("/conflicts", response_model=list[queries.ConflictView])
def list_conflicts(
    ctx: Context, run_id: uuid.UUID | None = None, limit: Limit = 100
) -> list[queries.ConflictView]:
    with session_scope(ctx.session_factory) as session:
        return queries.conflicts(session, run_id=run_id, limit=limit)


@router.get("/llm/analyses", response_model=list[queries.AnalysisView])
def list_analyses(
    ctx: Context,
    task: LLMTask | None = None,
    usable: bool | None = None,
    asset_id: uuid.UUID | None = None,
    limit: Limit = 50,
) -> list[queries.AnalysisView]:
    with session_scope(ctx.session_factory) as session:
        return queries.analyses(
            session,
            task=None if task is None else task.value,
            usable=usable,
            asset_id=asset_id,
            limit=limit,
        )


@router.get("/llm/analyses/{analysis_id}", response_model=queries.AnalysisView)
def get_analysis(ctx: Context, analysis_id: uuid.UUID) -> queries.AnalysisView:
    with session_scope(ctx.session_factory) as session:
        found = queries.analysis(session, analysis_id)
    if found is None:
        raise NotFoundError(f"LLM analysis {analysis_id} does not exist")
    return found


@router.get("/llm/requests", response_model=list[queries.RequestView])
def list_requests(
    ctx: Context, status: LLMRequestStatus | None = None, limit: Limit = 50
) -> list[queries.RequestView]:
    with session_scope(ctx.session_factory) as session:
        return queries.requests(
            session, status=None if status is None else status.value, limit=limit
        )


@router.get("/llm/usage")
def get_usage(ctx: Context, days: Annotated[int, Query(ge=1, le=366)] = 30) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.usage(session, ctx.settings, ctx.clock.now(), days)


@router.get("/evaluations", response_model=list[queries.EvaluationView])
def list_evaluations(ctx: Context, limit: Limit = 20) -> list[queries.EvaluationView]:
    with session_scope(ctx.session_factory) as session:
        return queries.evaluations(session, limit)


__all__ = ["ai", "router"]

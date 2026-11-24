"""Evidence program API (Phase 11A). **Paper trading only; evidence, not authority.**

Reads (GET): evidence protocols (pre-registered criteria), historical data audits and
readiness scorecards (backtest, out-of-sample and paper evidence kept apart, one
classification per strategy and for the portfolio).

Operator-gated writes (POST, ``X-Atlas-Operator-Token``): register a protocol version, run
a data audit, build a scorecard. In the paper environment protocol registration needs a
named operator credential (the shared token cannot fix the criteria). Nothing here submits
an order, changes a risk limit, enables, disables or promotes anything.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.market_data import require_operator
from app.api.v1.validation import Context
from app.core.config import Environment
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.security.operators import OperatorIdentity
from app.database.sessions import session_scope
from app.evidence import queries
from app.evidence import service as evidence
from app.evidence.data_audit import AuditScope
from app.evidence.protocol import DEFAULT_PROTOCOL_NAME, EvidenceCriteria

router = APIRouter(prefix="/evidence", tags=["evidence"])
Operator = Annotated[OperatorIdentity, Depends(require_operator)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtocolBody(_Body):
    name: str = Field(default=DEFAULT_PROTOCOL_NAME, min_length=1, max_length=120)
    rationale: str = Field(min_length=3, max_length=4000)
    criteria: EvidenceCriteria = Field(default_factory=EvidenceCriteria)


class AuditBody(_Body):
    symbols: list[str] = Field(min_length=1, max_length=500)
    start: date
    end: date
    benchmark: str | None = "SPY"
    universe_mode: str = "EXPLICIT"
    universe_selected_on: date | None = None
    provider: str = "alpaca"
    feed: str = "iex"
    settle_seconds: int = Field(default=3600, ge=60, le=57_600)


class ScorecardBody(_Body):
    protocol_id: uuid.UUID
    strategy_runs: dict[str, uuid.UUID] = Field(default_factory=dict)
    portfolio_run_id: uuid.UUID | None = None
    data_audit_id: uuid.UUID | None = None
    account_name: str | None = None
    extra_strategies: list[str] = Field(default_factory=list, max_length=50)


@router.get("/criteria/default")
def default_criteria() -> dict[str, Any]:
    c = EvidenceCriteria()
    return {"criteria": c.as_json(), "criteria_sha256": c.sha256()}


@router.get("/protocols")
def list_protocols(ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.protocols(session)


@router.get("/protocols/{protocol_id}")
def get_protocol(protocol_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.protocol_detail(session, protocol_id)


@router.post("/protocols", status_code=status.HTTP_201_CREATED)
def register_protocol(
    body: ProtocolBody, ctx: Context, identity: Operator, request: Request
) -> dict[str, Any]:
    """Fix a new version of the promotion and rejection criteria (append-only)."""
    if request.app.state.settings.environment is Environment.PAPER and not identity.named:
        raise ForbiddenError("registering an evidence protocol needs a named operator credential")
    with session_scope(ctx.session_factory) as session:
        row = evidence.register_protocol(
            session,
            body.criteria,
            name=body.name,
            rationale=body.rationale,
            actor=identity.actor,
            now=ctx.clock.now(),
        )
        return queries.protocol_view(row, full=True)


@router.get("/audits")
def list_audits(
    ctx: Context, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.audits(session, limit)


@router.get("/audits/{audit_id}")
def get_audit(audit_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.audit_detail(session, audit_id)


@router.post("/audits", status_code=status.HTTP_201_CREATED)
def run_audit(body: AuditBody, ctx: Context, identity: Operator) -> dict[str, Any]:
    scope = AuditScope(
        symbols=tuple(body.symbols),
        start=body.start,
        end=body.end,
        benchmark=body.benchmark,
        provider=body.provider,
        feed=body.feed,
        known_at=ctx.clock.now(),
        settle_seconds=body.settle_seconds,
        universe_mode=body.universe_mode,
        universe_selected_on=body.universe_selected_on,
    )
    with session_scope(ctx.session_factory) as session:
        row = evidence.run_audit(session, scope, actor=identity.actor, now=ctx.clock.now())
        return queries.audit_view(row, full=True)


@router.get("/scorecards")
def list_scorecards(
    ctx: Context, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.scorecards(session, limit)


@router.get("/scorecards/latest")
def latest_scorecard(ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        found = queries.latest_scorecard(session)
    if found is None:
        raise NotFoundError("no readiness scorecard has been built")
    return found


@router.get("/scorecards/{scorecard_id}")
def get_scorecard(scorecard_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.scorecard_detail(session, scorecard_id)


@router.post("/scorecards", status_code=status.HTTP_201_CREATED)
def build_scorecard(body: ScorecardBody, ctx: Context, identity: Operator) -> dict[str, Any]:
    """Judge the named validation runs and the paper account under one protocol."""
    request = evidence.ScorecardRequest(
        protocol_id=body.protocol_id,
        strategy_runs=dict(body.strategy_runs),
        portfolio_run_id=body.portfolio_run_id,
        data_audit_id=body.data_audit_id,
        account_name=body.account_name,
        extra_strategies=tuple(body.extra_strategies),
    )
    with session_scope(ctx.session_factory) as session:
        row = evidence.build_scorecard(session, request, actor=identity.actor, now=ctx.clock.now())
        return queries.scorecard_view(session, row, full=True)


__all__ = ["router"]

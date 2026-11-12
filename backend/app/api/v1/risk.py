"""Risk Center API (Phases 6B, 6C).

Read-only, except one fail-safe control: ``POST /risk/kill-switch/engage`` (operator token,
audited) can only *block* new entries. Nothing here changes a limit, releases the kill
switch, resets a breaker, assesses, reserves, consumes, releases a reservation, approves or
submits anything: configuration changes, resets, releases and reservation commands are
operator CLI actions or service interfaces (decisions D-146, D-155). Without
``account_id`` the primary paper account is used.

Reservations (Phase 6C): active and historical reservations, their events (rejections,
expirations, releases, consumptions), reserved and available capacity, and reconciliation
runs. Reading is all the API does with them.

"Live" figures are an evaluation that writes nothing; recorded breaker state, snapshots,
assessments and configuration history come from the append-only risk tables.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.market_data import require_trading_operator
from app.core.exceptions import DependencyUnavailableError
from app.core.security.operators import OperatorIdentity
from app.database.sessions import session_scope
from app.portfolio.queries import resolve_account
from app.risk import governance, queries
from app.risk.enums import AssessmentResult, BreakerType, ReservationEventType, ReservationStatus
from app.risk.reservations import queries as reservation_queries
from app.risk.runtime import RiskContext
from app.risk.state import evaluate

router = APIRouter(prefix="/risk", tags=["risk"])
_lock = threading.Lock()


def risk(request: Request) -> RiskContext:
    """The risk context, built on first use."""
    state = request.app.state
    context: RiskContext | None = getattr(state, "risk", None)
    if context is None:
        factory = getattr(state, "risk_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the risk engine is not configured")
        with _lock:
            context = getattr(state, "risk", None)
            if context is None:
                context = factory()
                state.risk = context
    return context


Context = Annotated[RiskContext, Depends(risk)]
AccountId = Annotated[uuid.UUID | None, Query(description="Defaults to the primary account")]
Limit = Annotated[int, Query(ge=1, le=200)]


class KillSwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)
    account_id: uuid.UUID | None = None


class KillSwitchResult(BaseModel):
    account_id: uuid.UUID
    engaged: bool
    already_engaged: bool
    release: str


@router.get("/state")
def current_state(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Current risk state: breakers, limits, live exposure and loss (not recorded), latest
    recorded snapshot and assessment counts."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        config = governance.active_configuration(session, account.id)
        live = evaluate(session, account, ctx.clock.now(), config, ctx.accounting, persist=False)
        return queries.overview(session, account, live)


@router.get("/limits")
def active_limits(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.limits(session, resolve_account(session, account_id))


@router.get("/exposure")
def exposure(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Live exposure from the ledger marked to fresh prices (nothing recorded)."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        config = governance.active_configuration(session, account.id)
        live = evaluate(session, account, ctx.clock.now(), config, ctx.accounting, persist=False)
        return queries.live(live)


@router.get("/exposure/snapshots")
def exposure_snapshots(
    ctx: Context, account_id: AccountId = None, limit: Limit = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.snapshots(session, resolve_account(session, account_id), limit)


@router.get("/assessments")
def list_assessments(
    ctx: Context,
    account_id: AccountId = None,
    result: AssessmentResult | None = None,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.assessments(session, resolve_account(session, account_id), limit, result)


@router.get("/assessments/{assessment_id}")
def get_assessment(ctx: Context, assessment_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.assessment(session, assessment_id)


@router.get("/circuit-breakers")
def circuit_breakers(ctx: Context, account_id: AccountId = None) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.breakers(session, resolve_account(session, account_id))


@router.get("/circuit-breakers/events")
def circuit_breaker_events(
    ctx: Context,
    account_id: AccountId = None,
    breaker: BreakerType | None = None,
    limit: Limit = 100,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.breaker_events(session, resolve_account(session, account_id), limit, breaker)


@router.get("/configurations")
def configuration_history(
    ctx: Context, account_id: AccountId = None, limit: Limit = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.configurations(session, resolve_account(session, account_id), limit)


@router.get("/events")
def risk_events(
    ctx: Context, account_id: AccountId = None, limit: Limit = 100
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.events(session, resolve_account(session, account_id), limit)


@router.get("/reservations")
def list_reservations(
    ctx: Context,
    account_id: AccountId = None,
    status: ReservationStatus | None = None,
    active: bool = False,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    """Reservations, newest first (``active=true``: only those holding capacity)."""
    with session_scope(ctx.session_factory) as session:
        return reservation_queries.reservations(
            session, resolve_account(session, account_id), limit, status, active
        )


@router.get("/reservations/capacity")
def reservation_capacity(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Reserved risk and exposure, and what is available after positions and reservations
    (live evaluation, nothing recorded)."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        config = governance.active_configuration(session, account.id)
        live = evaluate(session, account, ctx.clock.now(), config, ctx.accounting, persist=False)
        return reservation_queries.capacity(session, account, live, config)


@router.get("/reservations/events")
def reservation_events(
    ctx: Context,
    account_id: AccountId = None,
    event_type: ReservationEventType | None = None,
    limit: Limit = 100,
) -> list[dict[str, Any]]:
    """Reservation lifecycle history (filter e.g. REJECTED, EXPIRED, RELEASED, CONSUMED)."""
    with session_scope(ctx.session_factory) as session:
        return reservation_queries.events(
            session, resolve_account(session, account_id), limit, event_type
        )


@router.get("/reservations/reconciliations")
def reservation_reconciliations(
    ctx: Context, account_id: AccountId = None, limit: Limit = 20
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return reservation_queries.reconciliations(
            session, resolve_account(session, account_id), limit
        )


@router.get("/reservations/{reservation_id}")
def get_reservation(ctx: Context, reservation_id: uuid.UUID) -> dict[str, Any]:
    """One reservation with its events, approval record, idempotency records and assessment."""
    with session_scope(ctx.session_factory) as session:
        return reservation_queries.reservation(session, reservation_id)


@router.post("/kill-switch/engage", response_model=KillSwitchResult)
def engage_kill_switch(
    ctx: Context,
    body: KillSwitchBody,
    who: Annotated[OperatorIdentity, Depends(require_trading_operator)],
) -> KillSwitchResult:
    """Engage the manual kill switch (operator credential, audited as that operator). It
    only blocks new entries; releasing it is an operator CLI action with a reason."""
    with session_scope(ctx.session_factory) as session:
        account_id = resolve_account(session, body.account_id).id
    engaged = ctx.service.engage_kill_switch(account_id, actor=who.actor, reason=body.reason)
    return KillSwitchResult(
        account_id=account_id,
        engaged=True,
        already_engaged=not engaged,
        release="python -m app.risk.cli kill-switch release --actor operator:<name> "
        "--reason <text>",
    )


__all__ = ["router"]

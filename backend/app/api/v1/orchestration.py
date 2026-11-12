"""Autonomous paper-trading orchestration API (Phases 10A-10B).

Reads: the trading control and its history, the orchestrator's status, trade intents and
scheduled jobs; (10B) the protection gate, operational alerts and metrics. Writes are
**fail-safe controls only** (audited, reason required), following the kill-switch precedent
(D-146, D-155). Since 10B each is attributed to the named operator whose credential was
presented; in the paper environment only named credentials issued with trading control may
use them (D-218). Acknowledging an alert needs any operator credential:

* ``POST /orchestration/controls/pause``: ENABLED -> PAUSED (no new entries).
* ``POST /orchestration/controls/disable``: ENABLED or PAUSED -> DISABLED.
* ``POST /orchestration/controls/emergency-stop``: any mode -> EMERGENCY_STOP, engages the
  risk kill switch and requests cancellation of every working ENTRY order. Exits and the
  position monitor keep running.

Nothing here can enable trading, resume it or clear an emergency: those loosen a control and
are operator CLI actions (``python -m app.orchestration.cli enable|resume|clear-emergency
--actor operator:<name> --reason ...``). Without ``account_id`` the primary paper account
is used. Paper trading only.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.market_data import require_operator, require_trading_operator
from app.core.exceptions import DependencyUnavailableError, NotFoundError
from app.core.security.operators import OperatorIdentity
from app.database.models.orchestration import TradeIntentRecord
from app.database.sessions import session_scope
from app.orchestration import controls, queries, reliability
from app.orchestration.config import get_orchestrator_settings
from app.orchestration.enums import ControlAction, IntentStatus, JobKind, JobStatus
from app.orchestration.protection import assess
from app.orchestration.runtime import ControlContext
from app.portfolio.queries import resolve_account

router = APIRouter(prefix="/orchestration", tags=["orchestration"])
_lock = threading.Lock()
API_ACTOR = "operator:api"  # the shared-token / development identity
Operator = Annotated[OperatorIdentity, Depends(require_operator)]
TradingOperator = Annotated[OperatorIdentity, Depends(require_trading_operator)]


def orchestration(request: Request) -> ControlContext:
    """The control context, built on first use."""
    state = request.app.state
    context: ControlContext | None = getattr(state, "orchestration", None)
    if context is None:
        factory = getattr(state, "orchestration_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the orchestration controls are not configured")
        with _lock:
            context = getattr(state, "orchestration", None)
            if context is None:
                context = factory()
                state.orchestration = context
    return context


Context = Annotated[ControlContext, Depends(orchestration)]
AccountId = Annotated[uuid.UUID | None, Query(description="Defaults to the primary account")]
Limit = Annotated[int, Query(ge=1, le=200)]


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)
    account_id: uuid.UUID | None = None


class ControlResult(BaseModel):
    account_id: uuid.UUID
    action: str
    changed: bool
    control: dict[str, Any]


class EmergencyResult(ControlResult):
    kill_switch_engaged_now: bool
    cancel_requested: list[str]
    cancel_failed: list[dict[str, str]]
    clear: str


def _switches() -> tuple[bool | None, float | None]:
    try:
        s = get_orchestrator_settings()
    except ValueError:  # invalid ORCHESTRATOR_* in the API's environment: unknown here
        return None, None
    return s.worker_enabled, s.tick_seconds


def _account(ctx: ControlContext, account_id: uuid.UUID | None) -> uuid.UUID:
    with session_scope(ctx.session_factory) as session:
        return resolve_account(session, account_id).id


@router.get("/status")
def status(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Trading control, orchestrator heartbeat and health, intent and job counts."""
    enabled, tick = _switches()
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return {
            "account_id": str(account.id),
            "account_name": account.name,
            **queries.overview(
                session, account.id, ctx.clock.now(), enabled=enabled, tick_seconds=tick
            ),
        }


@router.get("/controls/history")
def control_history(
    ctx: Context, account_id: AccountId = None, limit: Limit = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return controls.history(session, resolve_account(session, account_id).id, limit)


@router.get("/intents")
def list_intents(
    ctx: Context,
    account_id: AccountId = None,
    status: IntentStatus | None = None,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.intents(
            session, resolve_account(session, account_id).id, status=status, limit=limit
        )


@router.get("/intents/{intent_id}")
def get_intent(ctx: Context, intent_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        row = session.get(TradeIntentRecord, intent_id)
        if row is None:
            raise NotFoundError(f"trade intent {intent_id} does not exist")
        return queries.intent_view(row)


@router.get("/jobs")
def list_jobs(
    ctx: Context,
    kind: JobKind | None = None,
    status: JobStatus | None = None,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.jobs(session, kind=kind, status=status, limit=limit)


def _change(
    ctx: ControlContext, body: ControlBody, action: ControlAction, actor: str
) -> ControlResult:
    account_id = _account(ctx, body.account_id)
    result = ctx.controls.change(account_id, action, actor=actor, reason=body.reason)
    return ControlResult(
        account_id=account_id,
        action=action.value,
        changed=result.changed,
        control=result.state.as_dict(),
    )


@router.post("/controls/pause", response_model=ControlResult)
def pause(ctx: Context, body: ControlBody, who: TradingOperator) -> ControlResult:
    """ENABLED -> PAUSED: no new entries; exits and monitoring continue."""
    return _change(ctx, body, ControlAction.PAUSE, who.actor)


@router.post("/controls/disable", response_model=ControlResult)
def disable(ctx: Context, body: ControlBody, who: TradingOperator) -> ControlResult:
    """ENABLED or PAUSED -> DISABLED: no new entries; exits and monitoring continue."""
    return _change(ctx, body, ControlAction.DISABLE, who.actor)


@router.post("/controls/emergency-stop", response_model=EmergencyResult)
def emergency_stop(ctx: Context, body: ControlBody, who: TradingOperator) -> EmergencyResult:
    """Any mode -> EMERGENCY_STOP; engages the kill switch and requests cancellation of
    working ENTRY orders. Safe to repeat. Exits are never cancelled."""
    account_id = _account(ctx, body.account_id)
    report = ctx.controls.emergency_stop(account_id, actor=who.actor, reason=body.reason)
    return EmergencyResult(
        account_id=account_id,
        action=ControlAction.EMERGENCY_STOP.value,
        changed=report.changed,
        control=report.state,
        kill_switch_engaged_now=report.kill_switch_engaged_now,
        cancel_requested=report.cancel_requested,
        cancel_failed=report.cancel_failed,
        clear="python -m app.orchestration.cli clear-emergency --actor operator:<name> "
        "--reason <text>",
    )


# ---------------------------------------------------------------------- Phase 10B reads
@router.get("/protection")
def protection_status(ctx: Context) -> dict[str, Any]:
    """Who manages open positions right now, and whether new entries may be taken in."""
    settings = _settings()
    with session_scope(ctx.session_factory) as session:
        return assess(
            session,
            ctx.clock.now(),
            require_workers=True if settings is None else settings.require_protection_workers,
        ).as_dict()


@router.get("/alerts")
def list_alerts(
    ctx: Context,
    account_id: AccountId = None,
    open_only: Annotated[bool, Query(description="Only unacknowledged alerts")] = False,
    min_severity: Annotated[str, Query(pattern="^(INFO|WARNING|HIGH|CRITICAL)$")] = "HIGH",
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    """Operational alerts (``system_health_events``) with the action to take and their
    acknowledgements. Acknowledging never hides an alert from the history."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return reliability.alerts(
            session, account.id, min_severity=min_severity, open_only=open_only, limit=limit
        )


class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=3, max_length=500)


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(ctx: Context, alert_id: int, body: AckBody, who: Operator) -> dict[str, Any]:
    """Record that an operator saw an alert (append-only; the alert itself never changes)."""
    with session_scope(ctx.session_factory) as session:
        return reliability.acknowledge(
            session, alert_id, actor=who.actor, note=body.note, now=ctx.clock.now()
        )


@router.get("/metrics")
def metrics(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Operational metrics: orchestrator counters and tick latency, jobs and dead letters,
    outbox backlog, intents, worker liveness, open alerts."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return reliability.metrics(session, account.id, ctx.clock.now())


def _settings() -> Any:
    try:
        return get_orchestrator_settings()
    except ValueError:
        return None


__all__ = ["API_ACTOR", "router"]

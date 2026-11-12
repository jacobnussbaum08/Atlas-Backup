"""Read models of the orchestration tables (Phase 10A). Read-only; error texts redacted."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security.redaction import redact_text
from app.database.models.orchestration import (
    OrchestrationJobRecord,
    OrchestratorStatusRecord,
    TradeIntentRecord,
)
from app.events.outbox import backlog
from app.monitoring.workers import WorkerHealth, heartbeat_health, orchestrator_stale_after
from app.orchestration import controls
from app.orchestration.enums import IntentStatus, JobKind, JobStatus
from app.orchestration.orchestrator import STATUS_NAME


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _error(text: str | None) -> str | None:
    return redact_text(text)[:500] if text else None


def orchestrator_status(
    session: Session, now: datetime, *, enabled: bool | None, tick_seconds: float | None
) -> dict[str, Any]:
    row = session.get(OrchestratorStatusRecord, STATUS_NAME)
    threshold = orchestrator_stale_after(tick_seconds)
    # The worker's own record decides; the API's environment only explains a missing row.
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=None if row is None else row.heartbeat_at,
        stale_after=threshold,
        enabled=None,
        state=None if row is None else row.state.value,
    )
    if row is None and enabled is False:
        health, detail = WorkerHealth.DISABLED, "ORCHESTRATOR_WORKER_ENABLED is false."
    return {
        "health": health.value,
        "detail": detail,
        "worker_enabled_in_api_env": enabled,
        "stale_after_seconds": threshold.total_seconds(),
        "worker_id": None if row is None else row.worker_id,
        "state": None if row is None else row.state.value,
        "trading_mode_seen": None
        if row is None or row.trading_mode is None
        else row.trading_mode.value,
        "heartbeat_at": None if row is None else _iso(row.heartbeat_at),
        "heartbeat_age_seconds": None
        if row is None
        else round((now - row.heartbeat_at).total_seconds(), 1),
        "started_at": None if row is None else _iso(row.started_at),
        "stopped_at": None if row is None else _iso(row.stopped_at),
        "last_tick_at": None if row is None else _iso(row.last_tick_at),
        "last_tick_ms": None if row is None else row.last_tick_ms,
        "counters": {} if row is None else dict(row.counters),
        "last_error": None if row is None else _error(row.last_error),
        "last_error_at": None if row is None else _iso(row.last_error_at),
    }


def intent_counts(session: Session, account_id: uuid.UUID) -> dict[str, int]:
    counts = {s.value: 0 for s in IntentStatus}
    for status, n in session.execute(
        select(TradeIntentRecord.status, func.count())
        .where(TradeIntentRecord.portfolio_account_id == account_id)
        .group_by(TradeIntentRecord.status)
    ).all():
        counts[IntentStatus(status).value] = int(n)
    return counts


def job_counts(session: Session) -> dict[str, int]:
    counts = {s.value: 0 for s in JobStatus}
    for status, n in session.execute(
        select(OrchestrationJobRecord.status, func.count()).group_by(OrchestrationJobRecord.status)
    ).all():
        counts[JobStatus(status).value] = int(n)
    return counts


def overview(
    session: Session,
    account_id: uuid.UUID,
    now: datetime,
    *,
    enabled: bool | None,
    tick_seconds: float | None,
) -> dict[str, Any]:
    """The account's trading control, the orchestrator's status and the work counts."""
    state = controls.current(session, account_id)
    return {
        "simulated": True,
        "control": state.as_dict(),
        "orchestrator": orchestrator_status(
            session, now, enabled=enabled, tick_seconds=tick_seconds
        ),
        "intents": intent_counts(session, account_id),
        "jobs": job_counts(session),
        "outbox": backlog(session),
        "notes": [
            "Paper trading only: every order is a simulated paper order filled by the "
            "isolated paper broker from stored market data.",
            "New entries need the operator control ENABLED; it is DISABLED until an operator "
            "enables it (CLI). Exits, protective orders and monitoring run in every mode.",
            "Pause, disable and emergency stop are available here with the operator token; "
            "enable, resume and clear-emergency are operator CLI actions.",
        ],
    }


def intent_view(r: TradeIntentRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "account_id": str(r.portfolio_account_id),
        "ai_decision_id": str(r.ai_decision_id),
        "decision_run_id": str(r.decision_run_id),
        "asset_id": None if r.asset_id is None else str(r.asset_id),
        "symbol": r.symbol,
        "direction": r.direction,
        "status": r.status.value,
        "trading_mode": None if r.trading_mode is None else r.trading_mode.value,
        "reservation_id": None if r.reservation_id is None else str(r.reservation_id),
        "reservation_decision": r.reservation_decision,
        "order_id": None if r.order_id is None else str(r.order_id),
        "execution_request_id": None
        if r.execution_request_id is None
        else str(r.execution_request_id),
        "reasons": r.reasons,
        "error": _error(r.error),
        "decision_expires_at": _iso(r.decision_expires_at),
        "received_at": _iso(r.received_at),
        "decided_at": _iso(r.decided_at),
    }


def intents(
    session: Session,
    account_id: uuid.UUID,
    *,
    status: IntentStatus | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = select(TradeIntentRecord).where(TradeIntentRecord.portfolio_account_id == account_id)
    if status is not None:
        q = q.where(TradeIntentRecord.status == status)
    rows = session.scalars(
        q.order_by(TradeIntentRecord.received_at.desc(), TradeIntentRecord.id).limit(limit)
    ).all()
    return [intent_view(r) for r in rows]


def job_view(r: OrchestrationJobRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "kind": r.kind.value,
        "slot": r.slot,
        "status": r.status.value,
        "attempts": r.attempts,
        "reclaimed": r.reclaimed,
        "lease_owner": r.lease_owner,
        "lease_expires_at": _iso(r.lease_expires_at),
        "next_attempt_at": _iso(r.next_attempt_at),
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "result": r.result,
        "error": _error(r.error),
    }


def jobs(
    session: Session,
    *,
    kind: JobKind | None = None,
    status: JobStatus | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = select(OrchestrationJobRecord)
    if kind is not None:
        q = q.where(OrchestrationJobRecord.kind == kind)
    if status is not None:
        q = q.where(OrchestrationJobRecord.status == status)
    rows = session.scalars(
        q.order_by(
            OrchestrationJobRecord.created_at.desc(), OrchestrationJobRecord.id.desc()
        ).limit(limit)
    ).all()
    return [job_view(r) for r in rows]


__all__ = [
    "intent_counts",
    "intent_view",
    "intents",
    "job_counts",
    "job_view",
    "jobs",
    "orchestrator_status",
    "overview",
]

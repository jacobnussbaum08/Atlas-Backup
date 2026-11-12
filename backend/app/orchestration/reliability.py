"""Read models for operations (Phase 10B): alerts with their actions and acknowledgements,
and operational metrics. Read-only except :func:`acknowledge` (an append-only row)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.security.redaction import redact_text
from app.database.models.orchestration import (
    OrchestrationJobRecord,
    OrchestratorStatusRecord,
    TradeIntentRecord,
)
from app.database.models.reliability import AlertAcknowledgementRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.events.outbox import backlog
from app.monitoring import heartbeats
from app.orchestration.alerts import action_for
from app.orchestration.enums import JobStatus
from app.orchestration.protection import assess
from app.risk.enums import Severity

SEVERITY_ORDER = [Severity.INFO, Severity.WARNING, Severity.HIGH, Severity.CRITICAL]


def _at_least(level: str) -> list[Severity]:
    wanted = Severity(level)
    return SEVERITY_ORDER[SEVERITY_ORDER.index(wanted) :]


def _ack_view(a: AlertAcknowledgementRecord) -> dict[str, Any]:
    return {"actor": a.actor, "note": a.note, "at": a.acknowledged_at.isoformat()}


def alert_view(
    e: SystemHealthEventRecord, acks: list[AlertAcknowledgementRecord]
) -> dict[str, Any]:
    details = dict(e.details or {})
    return {
        "id": e.id,
        "component": e.component,
        "event_type": e.event_type,
        "severity": e.severity.value,
        "summary": redact_text(e.summary),
        "subject_kind": e.subject_kind,
        "subject_id": e.subject_id,
        "occurred_at": e.occurred_at.isoformat(),
        "action": details.get("action") or action_for(e.event_type),
        "acknowledged": bool(acks),
        "acknowledgements": [_ack_view(a) for a in acks],
    }


def alerts(
    session: Session,
    account_id: uuid.UUID,
    *,
    min_severity: str = "HIGH",
    open_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    acked = select(AlertAcknowledgementRecord.health_event_id)
    query = select(SystemHealthEventRecord).where(
        SystemHealthEventRecord.severity.in_(_at_least(min_severity)),
        (SystemHealthEventRecord.portfolio_account_id == account_id)
        | SystemHealthEventRecord.portfolio_account_id.is_(None),
    )
    if open_only:
        query = query.where(SystemHealthEventRecord.id.notin_(acked))
    rows = session.scalars(
        query.order_by(
            SystemHealthEventRecord.occurred_at.desc(), SystemHealthEventRecord.id.desc()
        ).limit(limit)
    ).all()
    ids = [r.id for r in rows]
    by_event: dict[int, list[AlertAcknowledgementRecord]] = {}
    if ids:
        for a in session.scalars(
            select(AlertAcknowledgementRecord)
            .where(AlertAcknowledgementRecord.health_event_id.in_(ids))
            .order_by(AlertAcknowledgementRecord.id)
        ).all():
            by_event.setdefault(a.health_event_id, []).append(a)
    return [alert_view(r, by_event.get(r.id, [])) for r in rows]


def acknowledge(
    session: Session, alert_id: int, *, actor: str, note: str, now: datetime
) -> dict[str, Any]:
    event = session.get(SystemHealthEventRecord, alert_id)
    if event is None:
        raise NotFoundError(f"alert {alert_id} does not exist")
    if not actor.startswith("operator:"):
        raise InvalidRequestError("only an operator may acknowledge an alert")
    session.add(
        AlertAcknowledgementRecord(
            health_event_id=alert_id, actor=actor, note=note.strip(), acknowledged_at=now
        )
    )
    session.flush()
    acks = list(
        session.scalars(
            select(AlertAcknowledgementRecord)
            .where(AlertAcknowledgementRecord.health_event_id == alert_id)
            .order_by(AlertAcknowledgementRecord.id)
        ).all()
    )
    return alert_view(event, acks)


def metrics(session: Session, account_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    status = session.scalars(
        select(OrchestratorStatusRecord).order_by(OrchestratorStatusRecord.heartbeat_at.desc())
    ).first()
    jobs = {
        s.value: int(n)
        for s, n in session.execute(
            select(OrchestrationJobRecord.status, func.count()).group_by(
                OrchestrationJobRecord.status
            )
        ).all()
    }
    intents = {
        s.value: int(n)
        for s, n in session.execute(
            select(TradeIntentRecord.status, func.count())
            .where(TradeIntentRecord.portfolio_account_id == account_id)
            .group_by(TradeIntentRecord.status)
        ).all()
    }
    day_ago = now - timedelta(days=1)
    alerts_24h = {
        s.value: int(n)
        for s, n in session.execute(
            select(SystemHealthEventRecord.severity, func.count())
            .where(SystemHealthEventRecord.occurred_at >= day_ago)
            .group_by(SystemHealthEventRecord.severity)
        ).all()
    }
    open_high = int(
        session.scalar(
            select(func.count())
            .select_from(SystemHealthEventRecord)
            .where(
                SystemHealthEventRecord.severity.in_([Severity.HIGH, Severity.CRITICAL]),
                SystemHealthEventRecord.occurred_at >= now - timedelta(days=7),
                SystemHealthEventRecord.id.notin_(
                    select(AlertAcknowledgementRecord.health_event_id)
                ),
            )
        )
        or 0
    )
    monitor = heartbeats.liveness(session, heartbeats.POSITION_MONITOR, now)
    counters = dict(status.counters or {}) if status else {}
    return {
        "at": now.isoformat(),
        "simulated": True,
        "orchestrator": None
        if status is None
        else {
            "state": status.state.value,
            "heartbeat_age_seconds": round((now - status.heartbeat_at).total_seconds(), 1),
            "last_tick_ms": status.last_tick_ms,
            "entry_gate_open": status.entry_gate_open,
            "counters": counters,
        },
        "jobs": {s.value: jobs.get(s.value, 0) for s in JobStatus},
        "dead_letters": {
            "jobs": jobs.get(JobStatus.DEAD.value, 0),
            "outbox": backlog(session)["counts"].get("DEAD", 0),
        },
        "outbox": backlog(session),
        "intents": intents,
        "position_monitor": {
            "alive": monitor.alive,
            "detail": monitor.detail,
            "heartbeat_age_seconds": monitor.age_seconds,
        },
        "protection": assess(session, now).as_dict(),
        "alerts_last_24h": alerts_24h,
        "unacknowledged_high_alerts_7d": open_high,
    }


__all__ = ["acknowledge", "alert_view", "alerts", "metrics"]

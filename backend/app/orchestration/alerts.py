"""Operational alerts (Phase 10B): ``system_health_events`` rows, de-duplicated, with the
action an operator should take.

An alert is raised at most once per (component, event type, subject) per ``repeat`` window:
a condition that persists is not re-reported on every tick. Alerts are never edited or
deleted; an operator acknowledges one with a separate append-only row
(``alert_acknowledgements``), which never hides it from the history.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.reservations import SystemHealthEventRecord
from app.risk.enums import Severity

COMPONENT = "orchestration"
DEFAULT_REPEAT = timedelta(hours=1)

# What an operator should do for each alert type (shown with the alert; docs/operations.md).
ACTIONS: dict[str, str] = {
    "POSITION_MONITOR_DOWN": "Start the position monitor (python -m app.workers.position_monitor). "
    "Until it runs, the orchestrator closes its entry gate and runs protective passes itself.",
    "PAPER_EXECUTION_DOWN": "Start the paper execution worker (python -m "
    "app.workers.paper_execution). The orchestrator delivers and simulates orders meanwhile.",
    "PAPER_EXECUTION_STOPPED": "The paper broker recorded a clean stop while positions or "
    "orders are open: start the paper execution worker; nothing fills until it runs.",
    "ENTRY_GATE_CLOSED": "New entries are refused until the workers that manage positions run "
    "again (see the reasons). Exits continue.",
    "FEED_STALE": "Check the market-data stream worker and the provider status. Stale positions "
    "cannot trigger exits until fresh data (or the recovery backfill) arrives.",
    "SESSION_NOT_READY": "Resolve the listed blockers before the open, or pause trading "
    "(python -m app.orchestration.cli pause).",
    "WORKER_UNHEALTHY": "Check the worker's logs and restart it.",
    "ORCHESTRATION_JOB_DEAD": "Read the job's error, fix the cause, then requeue it (python -m "
    "app.orchestration.cli jobs requeue --id <id>).",
    "ORCHESTRATION_JOB_STUCK": "A worker stopped or hung holding a job; it was reclaimed. "
    "Check that worker.",
    "ORCHESTRATION_SCHEDULE_GAP": "The orchestrator was not running; check why it stopped.",
    "TRADE_INTENT_DEAD": "Read the intent's error; nothing was reserved or ordered.",
    "RESERVATION_RECONCILIATION_FAILED": "Inspect the reservation reconciliation run; pause "
    "trading until the difference is explained.",
    "ACCOUNTING_RECONCILIATION_FAILED": "Inspect the ledger reconciliation; the "
    "RECONCILIATION_FAILURE breaker halts new entries until a later run passes.",
    "EXECUTION_RECONCILIATION_FAILED": "Inspect the execution reconciliation findings.",
}


def action_for(event_type: str) -> str | None:
    return ACTIONS.get(event_type)


def raise_once(
    session: Session,
    *,
    event_type: str,
    severity: Severity,
    summary: str,
    now: datetime,
    account_id: uuid.UUID | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
    repeat: timedelta = DEFAULT_REPEAT,
    component: str = COMPONENT,
) -> bool:
    """Record the alert unless the same one was recorded within ``repeat``. True when it
    was written now."""
    query = select(SystemHealthEventRecord.id).where(
        SystemHealthEventRecord.component == component,
        SystemHealthEventRecord.event_type == event_type,
        SystemHealthEventRecord.occurred_at > now - repeat,
    )
    if subject_id is not None:
        query = query.where(SystemHealthEventRecord.subject_id == subject_id)
    if session.scalar(query.limit(1)) is not None:
        return False
    action = action_for(event_type)
    session.add(
        SystemHealthEventRecord(
            component=component,
            event_type=event_type,
            severity=severity,
            portfolio_account_id=account_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            summary=summary[:500],
            details={**(details or {}), **({"action": action} if action else {})},
            occurred_at=now,
        )
    )
    session.flush()
    return True


__all__ = ["ACTIONS", "COMPONENT", "DEFAULT_REPEAT", "action_for", "raise_once"]

"""Position protection and the entry gate (Phase 10B, D-213/D-214).

Two workers manage what is already open: the **position monitor** (marks, stops, targets,
exits) and the **paper execution worker** (delivers orders to the simulated paper broker and
simulates their fills). Before 10B a crashed position monitor was invisible and positions
were then silently unmanaged. Now:

* **Liveness** is read from what each worker recorded: the monitor's ``worker_heartbeats``
  row (10B) and the paper broker's heartbeat (7A), judged by age.
* **Entry gate.** While either worker is not running, no new entry is taken in or delivered
  (intents resolve NOT_TRADED with ``ENTRY_GATE_CLOSED``): Atlas never opens a position it
  cannot manage. The gate never affects exits.
* **Backstop.** While the monitor is down and something is open, the orchestrator runs the
  monitor's own pass (same code, same actor, same idempotency keys, per-position account
  lock: safe alongside a monitor that comes back). While the paper execution worker has
  crashed (broker still RUNNING, heartbeat stale) and orders or events are waiting, the
  orchestrator runs one delivery + simulation pass. It never writes either worker's
  heartbeat, so the outage stays visible. A broker that an operator stopped cleanly is not
  restarted: that is reported (CRITICAL) instead.
* **Alerts**: POSITION_MONITOR_DOWN, PAPER_EXECUTION_DOWN, PAPER_EXECUTION_STOPPED and
  ENTRY_GATE_CLOSED, de-duplicated (once per hour per subject), each with its action.

Nothing here can open or increase exposure: the backstop runs exits, cancellations and fills
of orders that already exist, exactly as the workers would.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models.execution import (
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerStateRecord,
    PaperPositionRecord,
)
from app.database.sessions import session_scope
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    BrokerRunState,
    OutboxStatus,
    PositionStatus,
)
from app.monitoring import heartbeats
from app.monitoring.workers import PAPER_BROKER_STALE_AFTER
from app.orchestration import alerts
from app.risk.enums import Severity

logger = get_logger(__name__)
INTENT_TOPICS = ("orchestration.intent",)


@dataclass(frozen=True)
class Assessment:
    at: datetime
    monitor_alive: bool
    monitor_detail: str
    broker_alive: bool
    broker_state: str | None
    broker_detail: str
    open_positions: int
    working_orders: int
    pending_events: int
    gate_open: bool
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "gate_open": self.gate_open,
            "reasons": self.reasons,
            "position_monitor": {"alive": self.monitor_alive, "detail": self.monitor_detail},
            "paper_execution": {
                "alive": self.broker_alive,
                "state": self.broker_state,
                "detail": self.broker_detail,
            },
            "open_positions": self.open_positions,
            "working_orders": self.working_orders,
            "pending_execution_events": self.pending_events,
        }


def _broker(session: Session, now: datetime) -> tuple[bool, str | None, str]:
    row = session.scalars(
        select(PaperBrokerStateRecord)
        .order_by(PaperBrokerStateRecord.last_heartbeat_at.desc().nulls_last())
        .limit(1)
    ).first()
    if row is None:
        return False, None, "the paper broker has never started"
    beat = row.last_heartbeat_at or row.updated_at
    age = (now - beat).total_seconds()
    if row.status is not BrokerRunState.RUNNING:
        return False, row.status.value, f"the paper broker is {row.status.value}"
    if age > PAPER_BROKER_STALE_AFTER.total_seconds():
        return (
            False,
            row.status.value,
            f"no paper broker heartbeat for {int(age)} s (limit "
            f"{int(PAPER_BROKER_STALE_AFTER.total_seconds())} s)",
        )
    return True, row.status.value, f"heartbeat {int(age)} s ago"


def assess(session: Session, now: datetime, *, require_workers: bool = True) -> Assessment:
    """The protection state at ``now`` (read-only)."""
    monitor = heartbeats.liveness(session, heartbeats.POSITION_MONITOR, now)
    broker_alive, broker_state, broker_detail = _broker(session, now)
    open_positions = int(
        session.scalar(
            select(func.count())
            .select_from(PaperPositionRecord)
            .where(PaperPositionRecord.status == PositionStatus.OPEN)
        )
        or 0
    )
    working = int(
        session.scalar(
            select(func.count())
            .select_from(OrderRecord)
            .where(OrderRecord.status.notin_(list(FINAL_ORDER_STATUSES)))
        )
        or 0
    )
    pending = int(
        session.scalar(
            select(func.count())
            .select_from(OutboxEventRecord)
            .where(
                OutboxEventRecord.status == OutboxStatus.PENDING,
                OutboxEventRecord.topic.notin_(INTENT_TOPICS),
            )
        )
        or 0
    )
    reasons: list[dict[str, Any]] = []
    if require_workers:
        if not monitor.alive:
            reasons.append(
                {
                    "code": "POSITION_MONITOR_DOWN",
                    "message": f"the position monitor is not running ({monitor.detail})",
                }
            )
        if not broker_alive:
            reasons.append(
                {
                    "code": "PAPER_EXECUTION_DOWN",
                    "message": f"the paper execution worker is not running ({broker_detail})",
                }
            )
    return Assessment(
        at=now,
        monitor_alive=monitor.alive,
        monitor_detail=monitor.detail,
        broker_alive=broker_alive,
        broker_state=broker_state,
        broker_detail=broker_detail,
        open_positions=open_positions,
        working_orders=working,
        pending_events=pending,
        gate_open=not reasons,
        reasons=reasons,
    )


@dataclass
class ProtectionReport:
    assessment: Assessment
    monitor_backstop: dict[str, Any] | None = None
    execution_backstop: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


class ProtectionGuard:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        account_id: uuid.UUID,
        *,
        require_workers: bool = True,
        monitor_pass: Callable[[], Any] | None = None,
        execution_pass: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._account = account_id
        self.require_workers = require_workers
        self._monitor_pass = monitor_pass
        self._execution_pass = execution_pass

    def gate(self, session: Session) -> Assessment:
        """The gate inside the caller's transaction (intent delivery)."""
        return assess(session, self._clock.now(), require_workers=self.require_workers)

    def current(self) -> Assessment:
        with session_scope(self._factory) as session:
            return self.gate(session)

    def run(self) -> ProtectionReport:
        """One tick: assess, alert, and run the backstops that are needed."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            a = assess(session, now, require_workers=self.require_workers)
            at_risk = a.open_positions > 0 or a.working_orders > 0
            if not a.monitor_alive:
                alerts.raise_once(
                    session,
                    event_type="POSITION_MONITOR_DOWN",
                    severity=Severity.CRITICAL if at_risk else Severity.WARNING,
                    summary=f"Position monitor not running: {a.monitor_detail}; "
                    f"{a.open_positions} open position(s)"
                    + ("; the orchestrator runs protective passes" if at_risk else ""),
                    now=now,
                    account_id=self._account,
                    subject_kind="worker",
                    subject_id=heartbeats.POSITION_MONITOR,
                    details={"open_positions": a.open_positions},
                )
            if not a.broker_alive:
                stopped = a.broker_state is not None and a.broker_state != "RUNNING"
                alerts.raise_once(
                    session,
                    event_type="PAPER_EXECUTION_STOPPED" if stopped else "PAPER_EXECUTION_DOWN",
                    severity=Severity.CRITICAL if at_risk else Severity.WARNING,
                    summary=f"Paper execution not running: {a.broker_detail}; "
                    f"{a.working_orders} working order(s), {a.pending_events} pending event(s)",
                    now=now,
                    account_id=self._account,
                    subject_kind="worker",
                    subject_id="paper-execution-worker",
                    details={
                        "working_orders": a.working_orders,
                        "pending_events": a.pending_events,
                    },
                )
            if not a.gate_open:
                alerts.raise_once(
                    session,
                    event_type="ENTRY_GATE_CLOSED",
                    severity=Severity.HIGH,
                    summary="New entries refused: " + "; ".join(r["message"] for r in a.reasons),
                    now=now,
                    account_id=self._account,
                    subject_kind="portfolio_account",
                    subject_id=str(self._account),
                    details={"reasons": a.reasons},
                )
        report = ProtectionReport(a)
        if not a.monitor_alive and at_risk and self._monitor_pass is not None:
            try:
                summary = self._monitor_pass()
                report.monitor_backstop = {
                    "positions": getattr(summary, "positions", None),
                    "triggers": getattr(summary, "triggers", None),
                    "exits_submitted": getattr(summary, "exits_submitted", None),
                    "errors": len(getattr(summary, "errors", []) or []),
                }
                logger.warning("orchestration.monitor_backstop", **report.monitor_backstop)
            except Exception as exc:
                report.errors.append(f"monitor backstop: {type(exc).__name__}: {exc}"[:500])
        crashed = not a.broker_alive and a.broker_state == "RUNNING"
        # At risk includes open positions: the monitor backstop above may just have
        # submitted an exit that the counts taken before it do not show yet.
        if crashed and (at_risk or a.pending_events > 0) and self._execution_pass is not None:
            try:
                report.execution_backstop = self._execution_pass()
                logger.warning("orchestration.execution_backstop", **report.execution_backstop)
            except Exception as exc:
                report.errors.append(f"execution backstop: {type(exc).__name__}: {exc}"[:500])
        return report


__all__ = ["Assessment", "ProtectionGuard", "ProtectionReport", "assess"]

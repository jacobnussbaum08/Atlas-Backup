"""Operator control commands (Phase 10A): enable, pause, resume, disable, emergency stop.

Each command is an operator action with a reason (see :mod:`app.orchestration.controls`).
An emergency stop is a sequence of idempotent steps, each in its own transaction, and is
safe to repeat until every step has happened:

1. record EMERGENCY_STOP (no new intent can be handled from this commit on: the intent
   handler reads the mode under the same account lock);
2. engage the risk engine's kill switch (6B; already engaged is fine);
3. request cancellation of every working ENTRY order (7A; idempotency key per order).

Exits are never cancelled: the position monitor keeps protecting and closing positions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.exceptions import AtlasError
from app.core.logging import get_logger
from app.database.sessions import session_scope
from app.execution.service import ExecutionService
from app.orchestration import controls
from app.orchestration.enums import ControlAction, TradingMode
from app.risk.service import RiskService

logger = get_logger(__name__)


@dataclass
class EmergencyReport:
    state: dict[str, Any]
    changed: bool
    kill_switch_engaged_now: bool
    cancel_requested: list[str] = field(default_factory=list)
    cancel_failed: list[dict[str, str]] = field(default_factory=list)


class ControlService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        *,
        risk: RiskService,
        execution: ExecutionService,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._risk = risk
        self._execution = execution

    def state(self, account_id: uuid.UUID) -> controls.ControlState:
        with session_scope(self._factory) as session:
            return controls.current(session, account_id)

    def change(
        self, account_id: uuid.UUID, action: ControlAction, *, actor: str, reason: str
    ) -> controls.ChangeResult:
        if action is ControlAction.EMERGENCY_STOP:
            raise controls.ControlError("use emergency_stop for an emergency stop")
        with session_scope(self._factory) as session:
            result = controls.change_in(
                session, account_id, action, actor=actor, reason=reason, at=self._clock.now()
            )
        logger.warning(
            "orchestration.trading_control",
            action=action.value,
            mode=result.state.mode.value,
            changed=result.changed,
            actor=actor,
        )
        return result

    def emergency_stop(self, account_id: uuid.UUID, *, actor: str, reason: str) -> EmergencyReport:
        with session_scope(self._factory) as session:
            result = controls.change_in(
                session,
                account_id,
                ControlAction.EMERGENCY_STOP,
                actor=actor,
                reason=reason,
                at=self._clock.now(),
            )
            sequence = result.state.sequence
        engaged = self._risk.engage_kill_switch(
            account_id, actor=actor, reason=f"emergency stop: {reason}"
        )
        report = EmergencyReport(result.state.as_dict(), result.changed, engaged)
        with session_scope(self._factory) as session:
            working = controls.working_entry_orders(session, account_id)
        for order_id in working:
            try:
                self._execution.request_cancel(
                    order_id,
                    idempotency_key=f"orch:emergency:{sequence}:{order_id}",
                    actor=actor,
                    reason=f"emergency stop: {reason}",
                )
                report.cancel_requested.append(str(order_id))
            except AtlasError as exc:  # e.g. it became final meanwhile; reported, not hidden
                report.cancel_failed.append({"order_id": str(order_id), "error": exc.message})
        logger.critical(
            "orchestration.emergency_stop",
            changed=result.changed,
            kill_switch_engaged_now=engaged,
            cancel_requested=len(report.cancel_requested),
            actor=actor,
        )
        return report


__all__ = ["ControlService", "EmergencyReport", "TradingMode"]

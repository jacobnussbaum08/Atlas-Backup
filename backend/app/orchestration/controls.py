"""Operator trading controls (Phase 10A, spec 24.2).

The trading mode of a paper account is the last ``trading_control_events`` row; with none
it is **DISABLED**. Only an authorized operator (``operator:<name>``, as the risk
governance requires) changes it, with a reason, and every change is an append-only,
gaplessly numbered event plus an audit log entry.

| Action           | From                      | To             |
|------------------|---------------------------|----------------|
| ENABLE           | DISABLED                  | ENABLED        |
| PAUSE            | ENABLED                   | PAUSED         |
| RESUME           | PAUSED                    | ENABLED        |
| DISABLE          | ENABLED, PAUSED           | DISABLED       |
| EMERGENCY_STOP   | any                       | EMERGENCY_STOP |
| CLEAR_EMERGENCY  | EMERGENCY_STOP            | DISABLED       |

An emergency stop also engages the risk engine's kill switch (6B) and requests
cancellation of every working ENTRY order of the account. Exits, protective orders and the
position monitor keep running: halting entries never prevents reducing risk. Clearing an
emergency returns to DISABLED, never straight to ENABLED, and does not release the kill
switch (that stays the risk CLI's explicit operator action). Nothing here changes a risk
limit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AtlasError
from app.database.models.audit_log import ActorType
from app.database.models.execution import OrderRecord
from app.database.models.orchestration import TradingControlEventRecord
from app.database.repositories.audit_log import AuditLogRepository
from app.execution.enums import OrderPurpose, OrderStatus
from app.orchestration.enums import ControlAction, TradingMode
from app.risk import governance
from app.risk.state import lock_account

ALLOWED: dict[ControlAction, tuple[frozenset[TradingMode], TradingMode]] = {
    ControlAction.ENABLE: (frozenset({TradingMode.DISABLED}), TradingMode.ENABLED),
    ControlAction.PAUSE: (frozenset({TradingMode.ENABLED}), TradingMode.PAUSED),
    ControlAction.RESUME: (frozenset({TradingMode.PAUSED}), TradingMode.ENABLED),
    ControlAction.DISABLE: (
        frozenset({TradingMode.ENABLED, TradingMode.PAUSED}),
        TradingMode.DISABLED,
    ),
    ControlAction.EMERGENCY_STOP: (frozenset(TradingMode), TradingMode.EMERGENCY_STOP),
    ControlAction.CLEAR_EMERGENCY: (
        frozenset({TradingMode.EMERGENCY_STOP}),
        TradingMode.DISABLED,
    ),
}
# Working entry orders an emergency stop cancels (risk-increasing orders not yet final).
WORKING = frozenset({OrderStatus.APPROVED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED})


class ControlError(AtlasError):
    """A trading-control change was refused (nothing changed)."""

    code = "trading_control_refused"
    http_status = 409


@dataclass(frozen=True)
class ControlState:
    account_id: uuid.UUID
    mode: TradingMode
    sequence: int
    since: datetime | None
    actor: str | None
    reason: str | None

    @property
    def entries_allowed(self) -> bool:
        return self.mode is TradingMode.ENABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": str(self.account_id),
            "mode": self.mode.value,
            "entries_allowed": self.entries_allowed,
            "sequence": self.sequence,
            "since": None if self.since is None else self.since.isoformat(),
            "actor": self.actor,
            "reason": self.reason,
            "default": self.sequence == 0,
        }


def current(session: Session, account_id: uuid.UUID) -> ControlState:
    """The account's trading mode; DISABLED when no operator ever changed it."""
    last = session.scalar(
        select(TradingControlEventRecord)
        .where(TradingControlEventRecord.portfolio_account_id == account_id)
        .order_by(TradingControlEventRecord.sequence.desc())
        .limit(1)
    )
    if last is None:
        return ControlState(account_id, TradingMode.DISABLED, 0, None, None, None)
    return ControlState(
        account_id, last.to_mode, last.sequence, last.occurred_at, last.actor, last.reason
    )


def history(session: Session, account_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(TradingControlEventRecord)
        .where(TradingControlEventRecord.portfolio_account_id == account_id)
        .order_by(TradingControlEventRecord.sequence.desc())
        .limit(limit)
    ).all()
    return [
        {
            "sequence": r.sequence,
            "action": r.action.value,
            "from_mode": r.from_mode.value,
            "to_mode": r.to_mode.value,
            "actor": r.actor,
            "reason": r.reason,
            "occurred_at": r.occurred_at.isoformat(),
            "details": r.details,
        }
        for r in rows
    ]


@dataclass(frozen=True)
class ChangeResult:
    state: ControlState
    changed: bool
    details: dict[str, Any]


def change_in(
    session: Session,
    account_id: uuid.UUID,
    action: ControlAction,
    *,
    actor: str,
    reason: str,
    at: datetime,
    details: dict[str, Any] | None = None,
) -> ChangeResult:
    """Record ``action`` under the account lock. Repeating an action that is already in
    effect (e.g. PAUSE while PAUSED) changes nothing and is not an error."""
    governance.require_operator(actor)
    if len((reason or "").strip()) < 3:
        raise ControlError("a reason is required")
    lock_account(session, account_id)
    state = current(session, account_id)
    allowed_from, to_mode = ALLOWED[action]
    if state.mode is to_mode:
        return ChangeResult(state, False, {})
    if state.mode not in allowed_from:
        raise ControlError(
            f"{action.value} is not allowed while trading is {state.mode.value} "
            f"(allowed from: {', '.join(sorted(m.value for m in allowed_from))})"
        )
    event = TradingControlEventRecord(
        portfolio_account_id=account_id,
        sequence=state.sequence + 1,
        action=action,
        from_mode=state.mode,
        to_mode=to_mode,
        actor=actor,
        reason=reason.strip(),
        details=details or {},
        occurred_at=at,
    )
    session.add(event)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=ActorType.USER,
        actor_id=actor,
        action=f"orchestration.trading_{action.value.lower()}",
        entity_type="portfolio_account",
        entity_id=str(account_id),
        reason=reason.strip(),
        previous_state={"mode": state.mode.value},
        new_state={"mode": to_mode.value},
        details={"sequence": event.sequence, "simulated": True, **(details or {})},
    )
    return ChangeResult(current(session, account_id), True, details or {})


def working_entry_orders(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(OrderRecord.id)
            .where(
                OrderRecord.portfolio_account_id == account_id,
                OrderRecord.purpose == OrderPurpose.ENTRY,
                OrderRecord.status.in_(sorted(WORKING)),
            )
            .order_by(OrderRecord.submitted_at)
        ).all()
    )


__all__ = [
    "ALLOWED",
    "ChangeResult",
    "ControlError",
    "ControlState",
    "change_in",
    "current",
    "history",
    "working_entry_orders",
]

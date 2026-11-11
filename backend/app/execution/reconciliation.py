"""Execution reconciliation (Phases 7A-7B): an independent cross-check of order state.

Read-only. For every order of an account (or of all accounts):

* the projection equals a replay of its events (status, filled, remaining, numbering);
* filled quantity equals the sum of its fills; each FILL event has exactly one fill;
* each fill's 6A journal entry moved exactly the filled quantity in the order's direction;
* ENTRY orders: the reservation consumption attributed to the order equals its filled
  quantity, and a final order's reservation holds nothing any more;
* the paper broker's book never shows less filled than the order; unapplied broker events
  are reported (a lagging delivery is a warning, not a discrepancy).

Phase 7B adds, per position and per simulated fill:

* a position's opened and closed quantities equal its entry and exit fills; its realized
  P/L equals the 6A ledger's realized P/L of its exit fills; its costs equal its fills';
* a closed position holds nothing and its entry order is final;
* per asset, the open positions' signed quantity equals the 6A ledger's position, unless
  the account also holds quantity that no order filled (controlled 6A test trades: a
  warning, not a discrepancy);
* every SIMULATION fill has its price/cost breakdown and was posted as SIMULATED_EXECUTION.

Unexplained findings write a CRITICAL ``system_health_events`` row. Nothing is repaired:
order history is append-only and every fix is a new, explained event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.enums import EntrySource
from app.core.clocks import Clock
from app.database.models.accounting import AssetQuantityRecord, JournalEntryRecord
from app.database.models.execution import (
    ExecutionFillDetailRecord,
    OrderEventRecord,
    OrderFillRecord,
    OrderRecord,
    PaperBrokerOrderRecord,
    PaperPositionRecord,
)
from app.database.models.reservations import (
    RiskReservationEventRecord,
    RiskReservationRecord,
    SystemHealthEventRecord,
)
from app.database.sessions import session_scope
from app.execution import state_machine
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    BrokerEventSource,
    OrderEventType,
    OrderPurpose,
    OrderStatus,
    PositionStatus,
)
from app.execution.portfolio import MOVEMENT_SIGN
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    ReservationEventType,
    Severity,
    TradeDirection,
)

ZERO = Decimal(0)


@dataclass
class ExecutionReconciliation:
    orders_checked: int = 0
    events_checked: int = 0
    positions_checked: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings


class ExecutionReconciler:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    def run(self, account_id: uuid.UUID | None = None) -> ExecutionReconciliation:
        with session_scope(self._factory) as session:
            result = self.run_in(session, account_id)
            if result.findings:
                session.add(
                    SystemHealthEventRecord(
                        component="paper_execution",
                        event_type="EXECUTION_RECONCILIATION_FAILED",
                        severity=Severity.CRITICAL,
                        portfolio_account_id=account_id,
                        subject_kind="execution_reconciliation",
                        subject_id=None,
                        summary=f"{len(result.findings)} unexplained execution finding(s)",
                        details={"findings": result.findings[:50]},
                        occurred_at=self._clock.now(),
                    )
                )
            return result

    def run_in(
        self, session: Session, account_id: uuid.UUID | None = None
    ) -> ExecutionReconciliation:
        out = ExecutionReconciliation()
        q = select(OrderRecord).order_by(OrderRecord.submitted_at, OrderRecord.id)
        if account_id is not None:
            q = q.where(OrderRecord.portfolio_account_id == account_id)
        for order in session.scalars(q):
            out.orders_checked += 1
            self._order(session, order, out)
        pq = select(PaperPositionRecord).order_by(PaperPositionRecord.opened_at)
        if account_id is not None:
            pq = pq.where(PaperPositionRecord.portfolio_account_id == account_id)
        positions = list(session.scalars(pq).all())
        for position in positions:
            out.positions_checked += 1
            self._position(session, position, out)
        self._ledger_positions(session, positions, out)
        return out

    @staticmethod
    def _position_finding(
        out: ExecutionReconciliation, position: PaperPositionRecord, code: str, message: str
    ) -> None:
        out.findings.append({"position_id": str(position.id), "code": code, "message": message})

    def _position(
        self, session: Session, position: PaperPositionRecord, out: ExecutionReconciliation
    ) -> None:
        entry_fills = list(
            session.scalars(
                select(OrderFillRecord).where(OrderFillRecord.order_id == position.entry_order_id)
            ).all()
        )
        exit_fills = list(
            session.scalars(
                select(OrderFillRecord)
                .join(OrderRecord, OrderRecord.id == OrderFillRecord.order_id)
                .where(
                    OrderRecord.parent_order_id == position.entry_order_id,
                    OrderRecord.purpose == OrderPurpose.EXIT,
                )
            ).all()
        )
        opened = sum((f.quantity for f in entry_fills), ZERO)
        closed = sum((f.quantity for f in exit_fills), ZERO)
        if (opened, closed) != (position.quantity_opened, position.quantity_closed):
            self._position_finding(
                out,
                position,
                "POSITION_QUANTITY_MISMATCH",
                f"fills give {opened} opened / {closed} closed, the position "
                f"{position.quantity_opened} / {position.quantity_closed}",
            )
        realized = ZERO
        if exit_fills:
            realized = Decimal(
                session.scalar(
                    select(func.coalesce(func.sum(AssetQuantityRecord.realized_pnl), 0)).where(
                        AssetQuantityRecord.entry_id.in_([f.journal_entry_id for f in exit_fills]),
                        AssetQuantityRecord.asset_id == position.asset_id,
                    )
                )
                or 0
            )
        if realized != position.realized_pnl:
            self._position_finding(
                out,
                position,
                "REALIZED_PNL_MISMATCH",
                f"the ledger realized {realized}, the position records {position.realized_pnl}",
            )
        fills = [*entry_fills, *exit_fills]
        commissions = sum((f.commission for f in fills), ZERO)
        fees = sum((f.fees for f in fills), ZERO)
        if (commissions, fees) != (position.commissions, position.fees):
            self._position_finding(
                out,
                position,
                "POSITION_COSTS_MISMATCH",
                f"fills cost {commissions} + {fees}, the position records "
                f"{position.commissions} + {position.fees}",
            )
        if position.status is PositionStatus.CLOSED:
            entry = session.get(OrderRecord, position.entry_order_id)
            if (
                position.open_quantity != ZERO
                or entry is None
                or entry.status not in FINAL_ORDER_STATUSES
            ):
                self._position_finding(
                    out,
                    position,
                    "CLOSED_NOT_FLAT",
                    "a closed position holds quantity or its entry order is still working",
                )
        for f in fills:
            if f.source is not BrokerEventSource.SIMULATION:
                continue
            detail = session.get(ExecutionFillDetailRecord, f.broker_event_id)
            entry_source = session.scalar(
                select(JournalEntryRecord.source).where(JournalEntryRecord.id == f.journal_entry_id)
            )
            if detail is None or entry_source is not EntrySource.SIMULATED_EXECUTION:
                self._position_finding(
                    out,
                    position,
                    "SIMULATED_FILL_UNTRACED",
                    f"simulated fill {f.id} lacks its breakdown or was not posted as "
                    "SIMULATED_EXECUTION",
                )

    @staticmethod
    def _ledger_positions(
        session: Session, positions: list[PaperPositionRecord], out: ExecutionReconciliation
    ) -> None:
        expected: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
        for p in positions:
            sign = 1 if p.direction is TradeDirection.LONG else -1
            key = (p.portfolio_account_id, p.asset_id)
            expected[key] = expected.get(key, ZERO) + sign * p.open_quantity
        for (account, asset), quantity in expected.items():
            held = Decimal(
                session.scalar(
                    select(func.coalesce(func.sum(AssetQuantityRecord.quantity_delta), 0)).where(
                        AssetQuantityRecord.portfolio_account_id == account,
                        AssetQuantityRecord.asset_id == asset,
                    )
                )
                or 0
            )
            if held == quantity:
                continue
            filled = set(
                session.scalars(
                    select(OrderFillRecord.journal_entry_id).where(
                        OrderFillRecord.portfolio_account_id == account,
                        OrderFillRecord.asset_id == asset,
                    )
                ).all()
            )
            entries = session.scalars(
                select(AssetQuantityRecord.entry_id).where(
                    AssetQuantityRecord.portfolio_account_id == account,
                    AssetQuantityRecord.asset_id == asset,
                )
            ).all()
            record = {
                "account_id": str(account),
                "asset_id": str(asset),
                "code": "LEDGER_POSITION_MISMATCH",
                "message": f"the ledger holds {held}, the open positions {quantity}",
            }
            if any(e not in filled for e in entries):
                record["code"] = "LEDGER_HAS_EXTERNAL_MOVEMENTS"
                out.warnings.append(record)
            else:
                out.findings.append(record)

    @staticmethod
    def _finding(out: ExecutionReconciliation, order: OrderRecord, code: str, message: str) -> None:
        out.findings.append({"order_id": str(order.id), "code": code, "message": message})

    def _order(self, session: Session, order: OrderRecord, out: ExecutionReconciliation) -> None:
        events = session.scalars(
            select(OrderEventRecord)
            .where(OrderEventRecord.order_id == order.id)
            .order_by(OrderEventRecord.sequence)
        ).all()
        out.events_checked += len(events)
        status: OrderStatus | None = None
        filled = ZERO
        for expected, event in enumerate(events, start=1):
            if event.sequence != expected:
                self._finding(out, order, "EVENT_GAP", f"event {expected} is missing")
                break
            if event.from_status is not status or not state_machine.is_allowed(
                status, event.event_type, event.to_status
            ):
                self._finding(
                    out,
                    order,
                    "ILLEGAL_TRANSITION",
                    f"event {event.sequence}: {status} --{event.event_type.value}--> "
                    f"{event.to_status.value}",
                )
            if event.event_type is OrderEventType.FILL:
                filled += event.quantity
            status = event.to_status
        if status is not order.status or filled != order.filled_quantity:
            self._finding(
                out,
                order,
                "PROJECTION_MISMATCH",
                f"replay gives {status} / {filled}, the projection {order.status.value} / "
                f"{order.filled_quantity}",
            )
        if order.filled_quantity + order.remaining_quantity != order.quantity:
            self._finding(out, order, "QUANTITY_CONSERVATION", "filled + remaining != quantity")
        fills = session.scalars(
            select(OrderFillRecord).where(OrderFillRecord.order_id == order.id)
        ).all()
        if sum((f.quantity for f in fills), ZERO) != order.filled_quantity:
            self._finding(
                out, order, "FILLS_MISMATCH", "the fills do not sum to the filled quantity"
            )
        fill_events = sum(1 for e in events if e.event_type is OrderEventType.FILL)
        if fill_events != len(fills):
            self._finding(out, order, "FILL_EVENTS_MISMATCH", "fill events and fills differ")
        for f in fills:
            moved = session.scalar(
                select(func.coalesce(func.sum(AssetQuantityRecord.quantity_delta), 0)).where(
                    AssetQuantityRecord.entry_id == f.journal_entry_id,
                    AssetQuantityRecord.asset_id == f.asset_id,
                )
            )
            if Decimal(moved or 0) != MOVEMENT_SIGN[f.side] * f.quantity:
                self._finding(
                    out,
                    order,
                    "LEDGER_MISMATCH",
                    f"journal entry {f.journal_entry_id} moved {moved}",
                )
        if order.purpose is OrderPurpose.ENTRY:
            self._reservation(session, order, out)
        book = session.scalar(
            select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == order.id)
        )
        if book is not None:
            if book.filled_quantity < order.filled_quantity:
                self._finding(
                    out, order, "BROKER_BEHIND", "the order filled more than the paper broker did"
                )
            if book.last_event_sequence > order.applied_broker_sequence:
                out.warnings.append(
                    {
                        "order_id": str(order.id),
                        "code": "BROKER_EVENTS_PENDING",
                        "message": f"{book.last_event_sequence - order.applied_broker_sequence} "
                        "paper broker event(s) not applied yet",
                    }
                )

    def _reservation(
        self, session: Session, order: OrderRecord, out: ExecutionReconciliation
    ) -> None:
        consumed = session.scalar(
            select(func.coalesce(func.sum(RiskReservationEventRecord.quantity), 0)).where(
                RiskReservationEventRecord.reservation_id == order.reservation_id,
                RiskReservationEventRecord.event_type == ReservationEventType.CONSUMED,
                RiskReservationEventRecord.order_ref == str(order.id),
            )
        )
        if Decimal(consumed or 0) != order.filled_quantity:
            self._finding(
                out,
                order,
                "CONSUMPTION_MISMATCH",
                f"reservation consumed {consumed} for the order, it filled {order.filled_quantity}",
            )
        r = session.get(RiskReservationRecord, order.reservation_id)
        if r is None:  # pragma: no cover - foreign key
            return
        if order.status in FINAL_ORDER_STATUSES and r.status in ACTIVE_RESERVATION_STATUSES:
            self._finding(
                out,
                order,
                "RESERVATION_NOT_SETTLED",
                f"order is {order.status.value} but its reservation still holds "
                f"{r.remaining_quantity}",
            )


__all__ = ["ExecutionReconciler", "ExecutionReconciliation"]

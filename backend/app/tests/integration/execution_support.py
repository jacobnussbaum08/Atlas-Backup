"""Shared setup for the Phase 7A paper execution integration tests.

TEST FIXTURE material only (see ``reservation_support``): CONTROLLED_TEST accounts with
simulated capital, invented instruments with MOCK bars. Every order is a **simulated paper
order**; every fill is a **controlled test execution event** recorded at the isolated paper
broker (Phase 7A has no fill algorithm). Nothing reaches a brokerage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models.execution import (
    OrderEventRecord,
    OrderFillRecord,
    OrderRecord,
    OutboxEventRecord,
)
from app.database.sessions import session_scope
from app.events.outbox import DispatchSummary, OutboxDispatcher
from app.execution.enums import CancelMode, OrderPurpose, OrderSide, OrderType
from app.execution.models import ControlledFill, OrderRequest, SubmissionResult
from app.execution.portfolio import AccountingPositionLedger
from app.execution.service import ExecutionService
from app.paper_broker.controlled import ControlledPaperBroker
from app.risk.enums import ReservationStatus, TradeDirection
from app.tests.integration.reservation_support import Reservations, make_reservations

COORDINATOR = "system:execution-coordinator"
# Fixture reservations are OPERATOR-sourced: only an operator may execute them.
DESK = "operator:test-desk"
D = Decimal


PRICE_FIELDS = ("limit_price", "stop_price", "trail_amount", "trail_percent")


def _fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: (D(v) if k in PRICE_FIELDS and isinstance(v, str) else v) for k, v in fields.items()}


@dataclass
class Execution:
    rv: Reservations
    service: ExecutionService
    broker: ControlledPaperBroker
    dispatcher: OutboxDispatcher

    @property
    def factory(self) -> Any:
        return self.rv.factory

    @property
    def risk(self) -> Any:
        return self.rv.risk

    # ---------------------------------------------------------------- setup
    def account(self, name: str = "exec-test") -> uuid.UUID:
        account_id: uuid.UUID = self.risk.account(name)
        return account_id

    def asset(self, symbol: str, **kw: Any) -> uuid.UUID:
        asset_id: uuid.UUID = self.risk.asset(symbol, **kw)
        return asset_id

    def reservation(
        self,
        account: uuid.UUID,
        asset: uuid.UUID,
        key: str,
        *,
        entry: str = "99.70",
        stop: str = "94.70",
        direction: TradeDirection = TradeDirection.LONG,
        **extra: Any,
    ) -> uuid.UUID:
        res = self.rv.reserve(
            account, self.risk.proposal(asset, entry, stop, direction, **extra), key
        )
        assert res.decision is ReservationStatus.APPROVED, res.reasons
        return res.reservation_id

    def strategy_reservation(
        self, account: uuid.UUID, asset: uuid.UUID, key: str, *, entry: str = "99.70"
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """A reservation of a stored (TEST FIXTURE) strategy proposal: (reservation, proposal)."""
        from datetime import timedelta

        from app.risk import proposals
        from app.strategies.enums import StrategyFamily
        from app.tests.integration.ai_support import make_proposal, make_strategy

        risk = self.risk
        strategy_id = f"momentum_fixture_{key[-4:]}"
        version = make_strategy(risk.factory, strategy_id, StrategyFamily.MOMENTUM)
        pid = make_proposal(
            risk.factory,
            strategy_id=strategy_id,
            version_id=version,
            asset_id=asset,
            stored_at=risk.now() - timedelta(hours=1),
            cutoff=risk.now() - timedelta(hours=2),
            entry=entry,
            expires_at=risk.now() + timedelta(days=1),
        )
        with session_scope(risk.factory) as session:
            proposal = proposals.from_strategy_proposal(session, pid)
        res = self.rv.reserve(account, proposal, key)
        assert res.decision is ReservationStatus.APPROVED, res.reasons
        return res.reservation_id, pid

    # ---------------------------------------------------------------- orders
    def entry(
        self,
        account: uuid.UUID,
        reservation: uuid.UUID,
        quantity: str,
        order_type: OrderType = OrderType.MARKET,
        **fields: Any,
    ) -> OrderRequest:
        return OrderRequest(
            account_id=account,
            purpose=OrderPurpose.ENTRY,
            reservation_id=reservation,
            order_type=order_type,
            quantity=D(quantity),
            **_fields(fields),
        )

    def exit(
        self,
        account: uuid.UUID,
        parent: uuid.UUID,
        quantity: str,
        order_type: OrderType = OrderType.MARKET,
        **fields: Any,
    ) -> OrderRequest:
        return OrderRequest(
            account_id=account,
            purpose=OrderPurpose.EXIT,
            parent_order_id=parent,
            order_type=order_type,
            quantity=D(quantity),
            **_fields(fields),
        )

    def submit(self, request: OrderRequest, key: str, by: str = DESK) -> SubmissionResult:
        return self.service.submit(request, idempotency_key=key, requested_by=by)

    def open_order(
        self, account: uuid.UUID, reservation: uuid.UUID, quantity: str, key: str, **kw: Any
    ) -> uuid.UUID:
        """Submit an entry order and deliver it to the paper broker (-> OPEN)."""
        result = self.submit(self.entry(account, reservation, quantity, **kw), key)
        assert result.order_id is not None, result.reasons
        self.dispatch()
        return result.order_id

    def dispatch(self, limit: int = 200) -> DispatchSummary:
        return self.dispatcher.dispatch(limit=limit)

    def fill(
        self,
        order_id: uuid.UUID,
        quantity: str,
        price: str,
        ref: str,
        *,
        commission: str = "0",
        fees: str = "0",
        apply: bool = True,
    ) -> Any:
        return self.service.record_controlled_fill(
            ControlledFill(
                order_id=order_id,
                quantity=D(quantity),
                price=D(price),
                commission=D(commission),
                fees=D(fees),
                external_ref=ref,
            ),
            actor="system:execution-test",
            apply=apply,
        )

    def cancel(self, order_id: uuid.UUID, key: str, by: str = DESK) -> Any:
        return self.service.request_cancel(
            order_id, idempotency_key=key, actor=by, reason="test cancellation"
        )

    # ---------------------------------------------------------------- reads
    def order(self, order_id: uuid.UUID) -> OrderRecord:
        with session_scope(self.factory) as session:
            o = session.get(OrderRecord, order_id)
            assert o is not None
            session.expunge(o)
            return o

    def events(self, order_id: uuid.UUID) -> list[OrderEventRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(OrderEventRecord)
                .where(OrderEventRecord.order_id == order_id)
                .order_by(OrderEventRecord.sequence)
            ).all()
            session.expunge_all()
            return list(rows)

    def event_types(self, order_id: uuid.UUID) -> list[str]:
        return [e.event_type.value for e in self.events(order_id)]

    def fills(self, order_id: uuid.UUID) -> list[OrderFillRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(OrderFillRecord)
                .where(OrderFillRecord.order_id == order_id)
                .order_by(OrderFillRecord.sequence)
            ).all()
            session.expunge_all()
            return list(rows)

    def count(self, session: Session, model: Any, *where: Any) -> int:
        return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    def pending_outbox(self) -> int:
        with session_scope(self.factory) as session:
            return self.count(session, OutboxEventRecord, OutboxEventRecord.status == "PENDING")


def make_execution(
    engine: Any, *, cancel_mode: CancelMode = CancelMode.IMMEDIATE, **config: Any
) -> Execution:
    rv = make_reservations(engine)
    broker = ControlledPaperBroker(cancel_mode=cancel_mode)
    from app.execution.models import ExecutionConfig

    service = ExecutionService(
        rv.factory,
        rv.risk.clock,
        rv.service._accounting,
        broker=broker,
        reservations=rv.service,
        ledger=AccountingPositionLedger(rv.risk.books.ctx.posting),
        config=ExecutionConfig(**config),
    )
    with session_scope(rv.factory) as session:
        broker.start(session, rv.risk.clock.now())
    return Execution(rv, service, broker, service.dispatcher())


__all__ = ["COORDINATOR", "DESK", "D", "Execution", "OrderSide", "OrderType", "make_execution"]

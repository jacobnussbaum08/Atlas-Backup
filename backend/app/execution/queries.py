"""Read models of the paper execution engine (API and CLI). Nothing here writes.

Every payload carries ``simulated: true``: these are simulated paper orders handled by
the isolated paper broker, never brokerage activity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.database.models.execution import (
    ExecutionRequestRecord,
    OrderEventRecord,
    OrderFillRecord,
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerEventRecord,
    PaperBrokerOrderRecord,
)
from app.database.models.reservations import RiskReservationRecord
from app.events.outbox import backlog
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    LIVE_ORDER_STATUSES,
    ExecutionDecision,
    OrderEventType,
    OrderStatus,
)
from app.paper_broker.controlled import ControlledPaperBroker

GROUPS: dict[str, frozenset[OrderStatus]] = {
    "open": LIVE_ORDER_STATUSES,
    "closed": FINAL_ORDER_STATUSES,
    "filled": frozenset({OrderStatus.FILLED}),
    "partially_filled": frozenset({OrderStatus.PARTIALLY_FILLED}),
    "cancelled": frozenset({OrderStatus.CANCELLED}),
    "rejected": frozenset({OrderStatus.REJECTED}),
    "expired": frozenset({OrderStatus.EXPIRED}),
    "failed": frozenset({OrderStatus.FAILED}),
}
SIMULATED_NOTICE = (
    "Simulated paper order handled by the isolated Atlas paper broker. No brokerage is "
    "connected and no real order exists. Fills are simulated from stored market data under "
    "recorded execution-model assumptions."
)


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def order_row(o: OrderRecord) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "simulated": True,
        "account_id": str(o.portfolio_account_id),
        "asset_id": str(o.asset_id),
        "symbol": o.symbol,
        "asset_class": o.asset_class,
        "currency": o.quote_currency,
        "purpose": o.purpose.value,
        "side": o.side.value,
        "direction": o.direction.value,
        "order_type": o.order_type.value,
        "status": o.status.value,
        "quantity": _num(o.quantity),
        "filled_quantity": _num(o.filled_quantity),
        "remaining_quantity": _num(o.remaining_quantity),
        "average_fill_price": _num(o.average_fill_price),
        "limit_price": _num(o.limit_price),
        "stop_price": _num(o.stop_price),
        "trail_amount": _num(o.trail_amount),
        "trail_percent": _num(o.trail_percent),
        "protection_price": _num(o.protection_price),
        "quantity_increment": _num(o.quantity_increment),
        "price_increment": _num(o.price_increment),
        "reservation_id": str(o.reservation_id),
        "risk_assessment_id": str(o.risk_assessment_id),
        "parent_order_id": None if o.parent_order_id is None else str(o.parent_order_id),
        "strategy_proposal_id": None
        if o.strategy_proposal_id is None
        else str(o.strategy_proposal_id),
        "ai_decision_id": None if o.ai_decision_id is None else str(o.ai_decision_id),
        "broker_order_id": None if o.broker_order_id is None else str(o.broker_order_id),
        "status_reason": o.status_reason,
        "exit_reason": None if o.exit_reason is None else o.exit_reason.value,
        "requested_by": o.requested_by,
        "submitted_at": _ts(o.submitted_at),
        "expires_at": _ts(o.expires_at),
        "closed_at": _ts(o.closed_at),
        "updated_at": _ts(o.updated_at),
    }


def event_row(e: OrderEventRecord, symbol: str | None = None) -> dict[str, Any]:
    return {
        "id": e.id,
        "order_id": str(e.order_id),
        "symbol": symbol,
        "sequence": e.sequence,
        "event_type": e.event_type.value,
        "from_status": None if e.from_status is None else e.from_status.value,
        "to_status": e.to_status.value,
        "quantity": _num(e.quantity),
        "price": _num(e.price),
        "source": e.source.value,
        "actor": e.actor,
        "reason": e.reason,
        "broker_event_id": None if e.broker_event_id is None else str(e.broker_event_id),
        "broker_sequence": e.broker_sequence,
        "occurred_at": _ts(e.occurred_at),
        "details": e.details,
    }


def orders(
    session: Session,
    account_id: uuid.UUID,
    *,
    limit: int,
    status: OrderStatus | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    q = select(OrderRecord).where(OrderRecord.portfolio_account_id == account_id)
    if status is not None:
        q = q.where(OrderRecord.status == status)
    if group is not None:
        q = q.where(OrderRecord.status.in_(sorted(GROUPS[group])))
    rows = session.scalars(
        q.order_by(OrderRecord.submitted_at.desc(), OrderRecord.id).limit(limit)
    ).all()
    return [order_row(o) for o in rows]


def _reservation_link(session: Session, o: OrderRecord) -> dict[str, Any]:
    r = session.get(RiskReservationRecord, o.reservation_id)
    assert r is not None  # noqa: S101  # foreign key
    return {
        "reservation_id": str(r.id),
        "role": "CONSUMED_BY_FILLS" if o.purpose.value == "ENTRY" else "PARENT_LINK_ONLY",
        "status": r.status.value,
        "execution_link": r.execution_link.value,
        "order_ref": r.order_ref,
        "approved_quantity": _num(r.approved_quantity),
        "consumed_quantity": _num(r.consumed_quantity),
        "released_quantity": _num(r.released_quantity),
        "remaining_quantity": _num(r.remaining_quantity),
        "remaining_risk": _num(r.remaining_risk),
        "remaining_exposure": _num(r.remaining_exposure),
        "expires_at": _ts(r.expires_at),
    }


def order(session: Session, order_id: uuid.UUID) -> dict[str, Any]:
    o = session.get(OrderRecord, order_id)
    if o is None:
        raise NotFoundError(f"order {order_id} does not exist")
    events = session.scalars(
        select(OrderEventRecord)
        .where(OrderEventRecord.order_id == o.id)
        .order_by(OrderEventRecord.sequence)
    ).all()
    fills = session.scalars(
        select(OrderFillRecord)
        .where(OrderFillRecord.order_id == o.id)
        .order_by(OrderFillRecord.sequence)
    ).all()
    request = session.get(ExecutionRequestRecord, o.execution_request_id)
    book = session.scalar(
        select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == o.id)
    )
    broker_events = session.scalars(
        select(PaperBrokerEventRecord)
        .where(PaperBrokerEventRecord.client_order_id == o.id)
        .order_by(PaperBrokerEventRecord.sequence)
    ).all()
    outbox = session.scalars(
        select(OutboxEventRecord)
        .where(
            OutboxEventRecord.aggregate_type == "order",
            OutboxEventRecord.aggregate_id == str(o.id),
        )
        .order_by(OutboxEventRecord.id)
    ).all()
    exits = session.scalars(
        select(OrderRecord.id).where(OrderRecord.parent_order_id == o.id).order_by(OrderRecord.id)
    ).all()
    return {
        "notice": SIMULATED_NOTICE,
        "order": order_row(o),
        "events": [event_row(e, o.symbol) for e in events],
        "fills": [
            {
                "id": str(f.id),
                "sequence": f.sequence,
                "quantity": _num(f.quantity),
                "price": _num(f.price),
                "commission": _num(f.commission),
                "fees": _num(f.fees),
                "currency": f.currency,
                "source": f.source.value,
                "journal_entry_id": str(f.journal_entry_id),
                "reservation_event_id": f.reservation_event_id,
                "filled_at": _ts(f.filled_at),
            }
            for f in fills
        ],
        "reservation": _reservation_link(session, o),
        "execution_request": None
        if request is None
        else {
            "id": str(request.id),
            "idempotency_key": request.idempotency_key,
            "decision": request.decision.value,
            "requested_by": request.requested_by,
            "requested_at": _ts(request.requested_at),
        },
        "paper_broker": {
            "simulated": True,
            "book": None
            if book is None
            else {
                "broker_order_id": str(book.id),
                "status": book.status.value,
                "quantity": _num(book.quantity),
                "filled_quantity": _num(book.filled_quantity),
                "last_event_sequence": book.last_event_sequence,
                "received_at": _ts(book.received_at),
            },
            "events": [
                {
                    "id": str(b.id),
                    "sequence": b.sequence,
                    "event_type": b.event_type.value,
                    "source": b.source.value,
                    "quantity": _num(b.quantity),
                    "price": _num(b.price),
                    "reason": b.reason,
                    "occurred_at": _ts(b.occurred_at),
                    "applied": b.sequence <= o.applied_broker_sequence,
                }
                for b in broker_events
            ],
        },
        "outbox": [
            {
                "id": e.id,
                "topic": e.topic,
                "status": e.status.value,
                "attempts": e.attempts,
                "last_error": e.last_error,
                "created_at": _ts(e.created_at),
                "delivered_at": _ts(e.delivered_at),
            }
            for e in outbox
        ],
        "exit_order_ids": [str(x) for x in exits],
        "execution": _execution(session, o.id),
    }


def _execution(session: Session, order_id: uuid.UUID) -> dict[str, Any]:
    from app.execution.position_queries import order_execution  # imports this module

    return order_execution(session, order_id)


def events(
    session: Session,
    account_id: uuid.UUID,
    *,
    limit: int,
    event_type: OrderEventType | None = None,
) -> list[dict[str, Any]]:
    q = (
        select(OrderEventRecord, OrderRecord.symbol)
        .join(OrderRecord, OrderRecord.id == OrderEventRecord.order_id)
        .where(OrderRecord.portfolio_account_id == account_id)
    )
    if event_type is not None:
        q = q.where(OrderEventRecord.event_type == event_type)
    rows = session.execute(q.order_by(OrderEventRecord.id.desc()).limit(limit)).all()
    return [event_row(e, symbol) for e, symbol in rows]


def summary(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    rows = session.execute(
        select(OrderRecord.status, func.count())
        .where(OrderRecord.portfolio_account_id == account_id)
        .group_by(OrderRecord.status)
    ).all()
    counts = {s.value: 0 for s in OrderStatus if s is not OrderStatus.PROPOSED}
    for status, count in rows:
        counts[OrderStatus(status).value] = int(count)
    refused = int(
        session.scalar(
            select(func.count())
            .select_from(ExecutionRequestRecord)
            .where(
                ExecutionRequestRecord.portfolio_account_id == account_id,
                ExecutionRequestRecord.decision == ExecutionDecision.REFUSED,
            )
        )
        or 0
    )
    return {
        "notice": SIMULATED_NOTICE,
        "simulated": True,
        "account_id": str(account_id),
        "counts": counts,
        "open": sum(counts[s.value] for s in LIVE_ORDER_STATUSES),
        "refused_requests": refused,
    }


def execution_requests(
    session: Session,
    account_id: uuid.UUID,
    *,
    limit: int,
    decision: ExecutionDecision | None = None,
) -> list[dict[str, Any]]:
    q = select(ExecutionRequestRecord).where(
        ExecutionRequestRecord.portfolio_account_id == account_id
    )
    if decision is not None:
        q = q.where(ExecutionRequestRecord.decision == decision)
    rows = session.scalars(
        q.order_by(ExecutionRequestRecord.requested_at.desc(), ExecutionRequestRecord.id).limit(
            limit
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "idempotency_key": r.idempotency_key,
            "purpose": r.purpose.value,
            "decision": r.decision.value,
            "reservation_id": None if r.reservation_id is None else str(r.reservation_id),
            "reasons": r.reasons,
            "requested_by": r.requested_by,
            "requested_at": _ts(r.requested_at),
        }
        for r in rows
    ]


def broker_status(session: Session, broker: ControlledPaperBroker) -> dict[str, Any]:
    health = broker.health(session)
    latest = session.scalars(
        select(PaperBrokerEventRecord)
        .where(PaperBrokerEventRecord.broker_id == broker.broker_id)
        .order_by(PaperBrokerEventRecord.created_at.desc(), PaperBrokerEventRecord.id)
        .limit(10)
    ).all()
    return {
        "notice": "Simulated paper broker. It is not connected to any brokerage and has no "
        "live-trading mode.",
        "broker_id": health.broker_id,
        "simulated": health.simulated,
        "status": health.status.value,
        "accepting_orders": health.accepting_orders,
        "fill_model": health.fill_model,
        "working_orders": health.working_orders,
        "last_heartbeat_at": _ts(health.last_heartbeat_at),
        "last_recovery_at": _ts(health.last_recovery_at),
        "outbox": backlog(session),
        "recent_events": [
            {
                "id": str(e.id),
                "client_order_id": str(e.client_order_id),
                "sequence": e.sequence,
                "event_type": e.event_type.value,
                "source": e.source.value,
                "occurred_at": _ts(e.occurred_at),
            }
            for e in latest
        ],
    }


__all__ = [
    "GROUPS",
    "SIMULATED_NOTICE",
    "broker_status",
    "event_row",
    "events",
    "execution_requests",
    "order",
    "order_row",
    "orders",
    "summary",
]

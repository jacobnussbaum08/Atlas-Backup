"""Read models for simulated positions and execution analytics (Phase 7B). Read only.

Every figure comes from stored records: positions and their events, fills with their 6A
journal entries, the simulator's fill breakdowns and evaluations, and the execution-model
versions. Nothing is estimated here; where a value is unknown it is ``None``.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.database.models.execution import (
    ExecutionAttemptRecord,
    ExecutionFillDetailRecord,
    ExecutionModelRecord,
    OrderFillRecord,
    OrderRecord,
    PaperBrokerOrderRecord,
    PaperPositionRecord,
    PositionCloseRequestRecord,
    PositionEventRecord,
)
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    BrokerEventSource,
    OrderStatus,
    PositionStatus,
)
from app.execution.queries import order_row
from app.paper_broker.execution_model import DEFAULT_MODEL
from app.risk.enums import TradeDirection

POSITION_NOTICE = (
    "Simulated paper position of the isolated Atlas paper broker. Fills are simulated from "
    "stored market data under the recorded execution-model assumptions; they are not real "
    "executions and are not guaranteed to match what a real market would have filled."
)
ANALYTICS_NOTICE = (
    "Simulated execution analytics. Slippage, fill rates and costs describe the simulator's "
    "assumptions applied to stored market data, not real execution quality. The default "
    "assumptions are deliberately conservative, not unbiased."
)
ZERO = Decimal(0)


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def position_row(p: PaperPositionRecord, now: datetime | None = None) -> dict[str, Any]:
    net = p.realized_pnl - p.commissions - p.fees - p.borrow_accrued
    basis = p.entry_value if p.entry_value else None
    held_until = p.closed_at or now
    return {
        "id": str(p.id),
        "simulated": True,
        "account_id": str(p.portfolio_account_id),
        "asset_id": str(p.asset_id),
        "symbol": p.symbol,
        "asset_class": p.asset_class,
        "currency": p.currency,
        "direction": p.direction.value,
        "status": p.status.value,
        "entry_order_id": str(p.entry_order_id),
        "reservation_id": str(p.reservation_id),
        "strategy_proposal_id": None
        if p.strategy_proposal_id is None
        else str(p.strategy_proposal_id),
        "ai_decision_id": None if p.ai_decision_id is None else str(p.ai_decision_id),
        "strategy_id": p.strategy_id,
        "origin": p.origin,
        "quantity_opened": _num(p.quantity_opened),
        "quantity_closed": _num(p.quantity_closed),
        "open_quantity": _num(p.open_quantity),
        "average_entry_price": _num(p.average_entry_price),
        "average_exit_price": _num(p.average_exit_price),
        "current_price": _num(p.last_mark_price),
        "current_price_at": _ts(p.last_mark_at),
        "current_price_source": p.last_mark_source,
        "unrealized_pnl": _num(p.unrealized_pnl) if p.status is PositionStatus.OPEN else "0",
        "realized_pnl": _num(p.realized_pnl),
        "commissions": _num(p.commissions),
        "fees": _num(p.fees),
        "borrow_accrued": _num(p.borrow_accrued),
        "borrow_paid": _num(p.borrow_paid),
        "net_pnl": _num(net),
        "net_return": None if basis is None else _num((net / basis).quantize(Decimal("1e-8"))),
        "stop_price": _num(p.stop_price),
        "initial_stop_price": _num(p.initial_stop_price),
        "trailing_stop_price": _num(p.trailing_stop_price),
        "trail_amount": _num(p.trail_amount),
        "trail_percent": _num(p.trail_percent),
        "target_price": _num(p.target_price),
        "invalidation_price": _num(p.invalidation_price),
        "expires_at": _ts(p.expires_at),
        "trigger_reason": None if p.trigger_reason is None else p.trigger_reason.value,
        "triggered_at": _ts(p.triggered_at),
        "exit_reason": None if p.exit_reason is None else p.exit_reason.value,
        "monitor_status": p.monitor_status,
        "opened_at": _ts(p.opened_at),
        "closed_at": _ts(p.closed_at),
        "holding_seconds": None
        if held_until is None
        else int((held_until - p.opened_at).total_seconds()),
        "updated_at": _ts(p.updated_at),
    }


def positions(
    session: Session,
    account_id: uuid.UUID,
    *,
    status: PositionStatus | None = None,
    limit: int = 200,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    q = select(PaperPositionRecord).where(PaperPositionRecord.portfolio_account_id == account_id)
    if status is not None:
        q = q.where(PaperPositionRecord.status == status)
    order = (
        PaperPositionRecord.closed_at.desc()
        if status is PositionStatus.CLOSED
        else PaperPositionRecord.opened_at.desc()
    )
    rows = session.scalars(q.order_by(order, PaperPositionRecord.id).limit(limit)).all()
    return [position_row(p, now) for p in rows]


def _fill_rows(session: Session, order_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    if not order_ids:
        return []
    fills = session.scalars(
        select(OrderFillRecord)
        .where(OrderFillRecord.order_id.in_(order_ids))
        .order_by(OrderFillRecord.filled_at, OrderFillRecord.sequence)
    ).all()
    details = {
        d.broker_event_id: d
        for d in session.scalars(
            select(ExecutionFillDetailRecord).where(
                ExecutionFillDetailRecord.order_id.in_(order_ids)
            )
        ).all()
    }
    out = []
    for f in fills:
        d = details.get(f.broker_event_id)
        out.append(
            {
                "id": str(f.id),
                "order_id": str(f.order_id),
                "side": f.side.value,
                "quantity": _num(f.quantity),
                "price": _num(f.price),
                "commission": _num(f.commission),
                "fees": _num(f.fees),
                "source": f.source.value,
                "journal_entry_id": str(f.journal_entry_id),
                "filled_at": _ts(f.filled_at),
                "detail": None if d is None else fill_detail_row(d),
            }
        )
    return out


def fill_detail_row(d: ExecutionFillDetailRecord) -> dict[str, Any]:
    return {
        "basis": d.basis.value,
        "execution_model_id": d.execution_model_id,
        "reference_price": _num(d.reference_price),
        "touch_price": _num(d.touch_price),
        "arrival_price": _num(d.arrival_price),
        "spread": _num(d.spread),
        "slippage_bps": _num(d.slippage_bps),
        "impact_bps": _num(d.impact_bps),
        "shortfall_bps": _num(d.shortfall_bps),
        "liquidity": _num(d.liquidity),
        "participation": _num(d.participation),
        "commission": _num(d.commission),
        "regulatory_fees": _num(d.regulatory_fees),
        "exchange_fees": _num(d.exchange_fees),
        "gap": d.gap,
        "observations": d.observations,
        "assumptions": d.assumptions,
    }


def position(
    session: Session, position_id: uuid.UUID, now: datetime | None = None
) -> dict[str, Any]:
    p = session.get(PaperPositionRecord, position_id)
    if p is None:
        raise NotFoundError(f"position {position_id} does not exist")
    events = session.scalars(
        select(PositionEventRecord)
        .where(PositionEventRecord.position_id == p.id)
        .order_by(PositionEventRecord.sequence)
    ).all()
    orders = session.scalars(
        select(OrderRecord)
        .where(
            (OrderRecord.id == p.entry_order_id) | (OrderRecord.parent_order_id == p.entry_order_id)
        )
        .order_by(OrderRecord.submitted_at, OrderRecord.id)
    ).all()
    requests = session.scalars(
        select(PositionCloseRequestRecord)
        .where(PositionCloseRequestRecord.position_id == p.id)
        .order_by(PositionCloseRequestRecord.requested_at)
    ).all()
    return {
        "notice": POSITION_NOTICE,
        "position": position_row(p, now),
        "events": [
            {
                "sequence": e.sequence,
                "event_type": e.event_type.value,
                "occurred_at": _ts(e.occurred_at),
                "actor": e.actor,
                "reason": e.reason,
                "quantity": _num(e.quantity),
                "price": _num(e.price),
                "open_quantity_after": _num(e.open_quantity_after),
                "order_id": None if e.order_id is None else str(e.order_id),
                "journal_entry_id": None if e.journal_entry_id is None else str(e.journal_entry_id),
                "details": e.details,
            }
            for e in events
        ],
        "orders": [order_row(o) for o in orders],
        "fills": _fill_rows(session, [o.id for o in orders]),
        "close_requests": [
            {
                "id": str(r.id),
                "requested_by": r.requested_by,
                "reason": r.reason,
                "status": r.status.value,
                "requested_at": _ts(r.requested_at),
                "resolved_at": _ts(r.resolved_at),
                "outcome": r.outcome,
            }
            for r in requests
        ],
    }


def order_execution(session: Session, order_id: uuid.UUID) -> dict[str, Any]:
    """The simulator's view of one order: model, trigger/trailing state, evaluations, fills."""
    book = session.scalar(
        select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == order_id)
    )
    attempts = session.scalars(
        select(ExecutionAttemptRecord)
        .where(ExecutionAttemptRecord.order_id == order_id)
        .order_by(ExecutionAttemptRecord.id.desc())
        .limit(50)
    ).all()
    details = session.scalars(
        select(ExecutionFillDetailRecord)
        .where(ExecutionFillDetailRecord.order_id == order_id)
        .order_by(ExecutionFillDetailRecord.filled_at)
    ).all()
    return {
        "execution_model_id": None if book is None else book.execution_model_id,
        "arrival_price": None if book is None else _num(book.arrival_price),
        "triggered_at": None if book is None else _ts(book.triggered_at),
        "trigger_price": None if book is None else _num(book.trigger_price),
        "trailing_stop_price": None if book is None else _num(book.current_stop_price),
        "trail_reference_price": None if book is None else _num(book.trail_reference_price),
        "evaluations": 0 if book is None else book.evaluations,
        "last_evaluated_at": None if book is None else _ts(book.last_evaluated_at),
        "last_outcome": None
        if book is None or book.last_attempt_outcome is None
        else book.last_attempt_outcome.value,
        "last_code": None if book is None else book.last_attempt_code,
        "attempts": [
            {
                "id": a.id,
                "evaluated_at": _ts(a.evaluated_at),
                "outcome": a.outcome.value,
                "code": a.code,
                "message": a.message,
                "triggered": a.triggered,
                "fill_event_id": None if a.fill_event_id is None else str(a.fill_event_id),
                "market": a.market,
            }
            for a in attempts
        ],
        "fill_details": [
            {
                "broker_event_id": str(d.broker_event_id),
                "quantity": _num(d.quantity),
                "price": _num(d.price),
            }
            | fill_detail_row(d)
            for d in details
        ],
    }


def _stats(values: list[Decimal]) -> dict[str, str | None]:
    if not values:
        return {"count": "0", "mean": None, "median": None, "max": None, "min": None}
    return {
        "count": str(len(values)),
        "mean": _num((sum(values, ZERO) / len(values)).quantize(Decimal("0.0001"))),
        "median": _num(Decimal(statistics.median(values)).quantize(Decimal("0.0001"))),
        "max": _num(max(values)),
        "min": _num(min(values)),
    }


def analytics(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    details = session.scalars(
        select(ExecutionFillDetailRecord).where(
            ExecutionFillDetailRecord.portfolio_account_id == account_id
        )
    ).all()
    orders = session.scalars(
        select(OrderRecord).where(OrderRecord.portfolio_account_id == account_id)
    ).all()
    fills_per_order = Counter(
        session.scalars(
            select(OrderFillRecord.order_id).where(
                OrderFillRecord.portfolio_account_id == account_id,
                OrderFillRecord.source == BrokerEventSource.SIMULATION,
            )
        ).all()
    )
    by_type: dict[str, dict[str, int]] = {}
    for o in orders:
        if o.status not in FINAL_ORDER_STATUSES or o.status is OrderStatus.REJECTED:
            continue
        row = by_type.setdefault(
            o.order_type.value, {"final": 0, "filled": 0, "partial": 0, "unfilled": 0}
        )
        row["final"] += 1
        if o.status is OrderStatus.FILLED:
            row["filled"] += 1
        elif o.filled_quantity > 0:
            row["partial"] += 1
        else:
            row["unfilled"] += 1
    fill_rates = {
        t: {
            **row,
            "fill_rate": None if not row["final"] else _num(Decimal(row["filled"]) / row["final"]),
        }
        for t, row in sorted(by_type.items())
    }
    notional = sum((d.quantity * d.price for d in details), ZERO)
    commission = sum((d.commission for d in details), ZERO)
    regulatory = sum((d.regulatory_fees for d in details), ZERO)
    exchange = sum((d.exchange_fees for d in details), ZERO)
    borrow = Decimal(
        session.scalar(
            select(func.coalesce(func.sum(PaperPositionRecord.borrow_accrued), 0)).where(
                PaperPositionRecord.portfolio_account_id == account_id
            )
        )
        or 0
    )
    basis_counts = Counter(d.basis.value for d in details)
    outcome_codes = Counter(
        (a.outcome.value, a.code)
        for a in session.scalars(
            select(ExecutionAttemptRecord)
            .join(OrderRecord, OrderRecord.id == ExecutionAttemptRecord.order_id)
            .where(OrderRecord.portfolio_account_id == account_id)
        ).all()
    )
    model_ids = sorted({d.execution_model_id for d in details})
    return {
        "notice": ANALYTICS_NOTICE,
        "simulated": True,
        "fills": len(details),
        "orders_with_simulated_fills": len(fills_per_order),
        "multi_fill_orders": sum(1 for n in fills_per_order.values() if n > 1),
        "by_basis": dict(sorted(basis_counts.items())),
        "slippage_bps": _stats([d.slippage_bps for d in details if d.slippage_bps is not None]),
        "slippage_bps_by_side": {
            side: _stats(
                [
                    d.slippage_bps
                    for d in details
                    if d.slippage_bps is not None and d.side.value == side
                ]
            )
            for side in sorted({d.side.value for d in details})
        },
        "impact_bps": _stats([d.impact_bps for d in details]),
        "shortfall_bps": _stats([d.shortfall_bps for d in details if d.shortfall_bps is not None]),
        "participation": _stats([d.participation for d in details if d.participation is not None]),
        "gaps": sum(1 for d in details if d.gap),
        "fill_rates": fill_rates,
        "costs": {
            "notional": _num(notional),
            "commission": _num(commission),
            "regulatory_fees": _num(regulatory),
            "exchange_fees": _num(exchange),
            "borrow_accrued": _num(borrow),
            "total": _num(commission + regulatory + exchange + borrow),
            "cost_bps_of_notional": None
            if notional == 0
            else _num(
                ((commission + regulatory + exchange) / notional * 10000).quantize(Decimal("0.01"))
            ),
        },
        "evaluations": [
            {"outcome": outcome, "code": code, "count": n}
            for (outcome, code), n in outcome_codes.most_common(20)
        ],
        "execution_model_ids": model_ids,
        "current_model": {
            "code": DEFAULT_MODEL.code,
            "version": DEFAULT_MODEL.version,
            "sha256": DEFAULT_MODEL.definition_sha256(),
            "description": DEFAULT_MODEL.description,
            "rules": list(DEFAULT_MODEL.rules),
            "classes": DEFAULT_MODEL.definition()["classes"],
        },
        "limitations": [
            "IEX quotes and trades are a small share of consolidated US volume: displayed "
            "sizes and volumes understate real liquidity, so simulated fills are slower and "
            "smaller than a consolidated feed would suggest",
            "queue position of resting limits is unknown: only volume strictly through the "
            "limit fills them, at a participation share",
            "bar-based decisions never assume an intrabar path; a bar reaching a stop and a "
            "target is treated as the stop first",
            "the bar fallback uses an assumed spread, not an observed quote (labelled "
            "BAR_FALLBACK)",
        ],
    }


def models(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(ExecutionModelRecord).order_by(ExecutionModelRecord.id)).all()
    return [
        {
            "id": m.id,
            "code": m.code,
            "version": m.version,
            "simulated": m.simulated,
            "sha256": m.definition_sha256,
            "description": m.description,
            "definition": m.definition,
            "created_at": _ts(m.created_at),
            "current": m.definition_sha256 == DEFAULT_MODEL.definition_sha256(),
        }
        for m in rows
    ]


def position_summary(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    rows = session.scalars(
        select(PaperPositionRecord).where(PaperPositionRecord.portfolio_account_id == account_id)
    ).all()
    open_rows = [p for p in rows if p.status is PositionStatus.OPEN]
    closed = [p for p in rows if p.status is PositionStatus.CLOSED]
    net = [p.realized_pnl - p.commissions - p.fees - p.borrow_accrued for p in closed]
    return {
        "simulated": True,
        "open": len(open_rows),
        "closed": len(closed),
        "long": sum(1 for p in open_rows if p.direction is TradeDirection.LONG),
        "short": sum(1 for p in open_rows if p.direction is TradeDirection.SHORT),
        "unrealized_pnl": _num(sum((p.unrealized_pnl or ZERO for p in open_rows), ZERO)),
        "realized_pnl": _num(sum((p.realized_pnl for p in closed), ZERO)),
        "net_pnl_closed": _num(sum(net, ZERO)),
        "winners": sum(1 for n in net if n > 0),
        "losers": sum(1 for n in net if n < 0),
        "exit_reasons": dict(
            sorted(Counter(p.exit_reason.value for p in closed if p.exit_reason).items())
        ),
        "stale": sum(1 for p in open_rows if p.monitor_status == "STALE"),
    }


__all__ = [
    "ANALYTICS_NOTICE",
    "POSITION_NOTICE",
    "analytics",
    "models",
    "order_execution",
    "position",
    "position_row",
    "position_summary",
    "positions",
]

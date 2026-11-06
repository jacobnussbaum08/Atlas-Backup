"""Read models for reservations (Risk Center API and CLI, Phase 6C). Read-only.

Decimals are strings. "Capacity" combines a live evaluation (nothing recorded) with the
active reservations and the hard limits: what is still available after actual positions
*and* outstanding reservations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.database.models import PortfolioAccount
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationEventRecord,
    RiskReservationReconciliationRecord,
    RiskReservationRecord,
    RiskReservationRequestRecord,
    RiskReservationStateRecord,
)
from app.database.models.risk import RiskAssessmentRecord
from app.risk.config import RiskConfiguration
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    ReservationEventType,
    ReservationStatus,
    TradeDirection,
)
from app.risk.reservations.reconciliation import latest
from app.risk.state import EvaluatedState

NOTE = (
    "A reservation holds risk budget, exposure, capital and margin for an approved trade "
    "intent. It is not an order, not a fill and not a ledger transaction. Nothing here "
    "submits or executes anything (paper only; execution is Phase 7)."
)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _q(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.normalize():f}"


def _t(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _row(r: RiskReservationRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "account_id": str(r.portfolio_account_id),
        "idempotency_key": r.idempotency_key,
        "source": r.source.value,
        "strategy_proposal_id": None
        if r.strategy_proposal_id is None
        else str(r.strategy_proposal_id),
        "ai_decision_id": None if r.ai_decision_id is None else str(r.ai_decision_id),
        "assessment_id": None if r.assessment_id is None else str(r.assessment_id),
        "asset_id": str(r.asset_id),
        "symbol": r.symbol,
        "asset_class": r.asset_class,
        "direction": r.direction.value,
        "quote_currency": r.quote_currency,
        "functional_currency": r.functional_currency,
        "entry": _s(r.entry),
        "stop": _s(r.stop),
        "target": _s(r.target),
        "status": r.status.value,
        "active": r.status in ACTIVE_RESERVATION_STATUSES,
        "proposed_quantity": _q(r.proposed_quantity),
        "approved_quantity": _q(r.approved_quantity),
        "consumed_quantity": _q(r.consumed_quantity),
        "released_quantity": _q(r.released_quantity),
        "remaining_quantity": _q(r.remaining_quantity),
        "approved": {
            "risk": _s(r.approved_risk),
            "exposure": _s(r.approved_exposure),
            "capital": _s(r.approved_capital),
            "margin": _s(r.approved_margin),
        },
        "remaining": {
            "risk": _s(r.remaining_risk),
            "exposure": _s(r.remaining_exposure),
            "capital": _s(r.remaining_capital),
            "margin": _s(r.remaining_margin),
        },
        "fx_rate": _s(r.fx_rate),
        "execution_link": r.execution_link.value,
        "order_ref": r.order_ref,
        "expires_at": _t(r.expires_at),
        "expiry_deferred_at": _t(r.expiry_deferred_at),
        "reasons": r.reasons,
        "requested_by": r.requested_by,
        "requested_at": _t(r.requested_at),
        "updated_at": _t(r.updated_at),
    }


def _event(e: RiskReservationEventRecord, symbol: str | None = None) -> dict[str, Any]:
    return {
        "id": e.id,
        "reservation_id": str(e.reservation_id),
        "symbol": symbol,
        "account_sequence": e.account_sequence,
        "reservation_sequence": e.reservation_sequence,
        "event_type": e.event_type.value,
        "from_status": None if e.from_status is None else e.from_status.value,
        "to_status": e.to_status.value,
        "quantity": _q(e.quantity),
        "risk": _s(e.risk_amount),
        "exposure": _s(e.exposure_amount),
        "capital": _s(e.capital_amount),
        "margin": _s(e.margin_amount),
        "journal_entry_id": None if e.journal_entry_id is None else str(e.journal_entry_id),
        "order_ref": e.order_ref,
        "resolution": None if e.resolution is None else e.resolution.value,
        "actor": e.actor,
        "reason": e.reason,
        "occurred_at": _t(e.occurred_at),
        "details": e.details,
    }


def reservations(
    session: Session,
    account: PortfolioAccount,
    limit: int,
    status: ReservationStatus | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    q = select(RiskReservationRecord).where(
        RiskReservationRecord.portfolio_account_id == account.id
    )
    if status is not None:
        q = q.where(RiskReservationRecord.status == status)
    if active_only:
        q = q.where(RiskReservationRecord.status.in_(ACTIVE_RESERVATION_STATUSES))
    rows = session.scalars(
        q.order_by(RiskReservationRecord.requested_at.desc(), RiskReservationRecord.id).limit(limit)
    ).all()
    return [_row(r) for r in rows]


def reservation(session: Session, reservation_id: uuid.UUID) -> dict[str, Any]:
    r = session.get(RiskReservationRecord, reservation_id)
    if r is None:
        raise NotFoundError(f"risk reservation {reservation_id} does not exist")
    events = session.scalars(
        select(RiskReservationEventRecord)
        .where(RiskReservationEventRecord.reservation_id == r.id)
        .order_by(RiskReservationEventRecord.reservation_sequence)
    ).all()
    approval = session.scalar(
        select(RiskReservationApprovalRecord).where(
            RiskReservationApprovalRecord.reservation_id == r.id
        )
    )
    requests = session.scalars(
        select(RiskReservationRequestRecord)
        .where(RiskReservationRequestRecord.reservation_id == r.id)
        .order_by(RiskReservationRequestRecord.id)
    ).all()
    assessment = (
        None if r.assessment_id is None else session.get(RiskAssessmentRecord, r.assessment_id)
    )
    return _row(r) | {
        "events": [_event(e, r.symbol) for e in events],
        "approval": None
        if approval is None
        else {
            "id": str(approval.id),
            "approved_quantity": _q(approval.approved_quantity),
            "proposed_quantity": _q(approval.proposed_quantity),
            "entry": _s(approval.entry),
            "worst_entry_price": _s(approval.worst_entry_price),
            "stop": _s(approval.stop),
            "target": _s(approval.target),
            "potential_loss": _s(approval.potential_loss),
            "notional": _s(approval.notional),
            "approved_at": _t(approval.approved_at),
            "valid_until": _t(approval.valid_until),
            "approved_by": approval.approved_by,
            "content_sha256": approval.content_sha256,
        },
        "requests": [
            {
                "idempotency_key": q.idempotency_key,
                "command": q.command.value,
                "result_status": q.result_status.value,
                "actor": q.actor,
                "created_at": _t(q.created_at),
            }
            for q in requests
        ],
        "assessment": None
        if assessment is None
        else {
            "id": str(assessment.id),
            "result": assessment.result.value,
            "permitted_quantity": _q(assessment.permitted_quantity),
            "risk_budget": _s(assessment.risk_budget),
            "potential_loss": _s(assessment.potential_loss),
            "binding_constraint": assessment.binding_constraint,
            "assessed_at": _t(assessment.assessed_at),
        },
        "note": NOTE,
    }


def events(
    session: Session,
    account: PortfolioAccount,
    limit: int,
    event_type: ReservationEventType | None = None,
) -> list[dict[str, Any]]:
    e = RiskReservationEventRecord
    q = (
        select(e, RiskReservationRecord.symbol)
        .join(RiskReservationRecord, RiskReservationRecord.id == e.reservation_id)
        .where(e.portfolio_account_id == account.id)
    )
    if event_type is not None:
        q = q.where(e.event_type == event_type)
    rows = session.execute(q.order_by(e.account_sequence.desc()).limit(limit)).all()
    return [_event(row, symbol) for row, symbol in rows]


def reconciliations(
    session: Session, account: PortfolioAccount, limit: int
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RiskReservationReconciliationRecord)
        .where(RiskReservationReconciliationRecord.portfolio_account_id == account.id)
        .order_by(
            RiskReservationReconciliationRecord.finished_at.desc(),
            RiskReservationReconciliationRecord.created_at.desc(),
        )
        .limit(limit)
    ).all()
    return [_reconciliation(r) for r in rows]


def _reconciliation(r: RiskReservationReconciliationRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "status": r.status.value,
        "started_at": _t(r.started_at),
        "finished_at": _t(r.finished_at),
        "account_sequence": r.account_sequence,
        "reservations_checked": r.reservations_checked,
        "events_checked": r.events_checked,
        "unexplained": r.unexplained,
        "warnings": r.warnings,
        "findings": r.findings,
        "totals": r.totals,
        "actor": r.actor,
    }


def counts(session: Session, account: PortfolioAccount) -> dict[str, int]:
    rows = session.execute(
        select(RiskReservationRecord.status, func.count())
        .where(RiskReservationRecord.portfolio_account_id == account.id)
        .group_by(RiskReservationRecord.status)
    ).all()
    found = {status.value: int(n) for status, n in rows}
    return {
        s.value: found.get(s.value, 0)
        for s in ReservationStatus
        if s is not ReservationStatus.PENDING
    }


def capacity(
    session: Session,
    account: PortfolioAccount,
    live: EvaluatedState | None,
    config: RiskConfiguration | None,
) -> dict[str, Any]:
    """Reserved totals and what is still available after positions and reservations."""
    state = session.get(RiskReservationStateRecord, account.id)
    recon = latest(session, account.id)
    zero = Decimal(0)
    reserved = {
        "active_count": 0 if state is None else state.active_count,
        "risk": _s(zero if state is None else state.reserved_risk),
        "gross_exposure": _s(zero if state is None else state.reserved_exposure),
        "long_exposure": _s(zero if state is None else state.reserved_long_exposure),
        "short_exposure": _s(zero if state is None else state.reserved_short_exposure),
        "capital": _s(zero if state is None else state.reserved_capital),
        "margin": _s(zero if state is None else state.reserved_margin),
        "event_sequence": 0 if state is None else state.last_sequence,
    }
    available: dict[str, Any] = {"computed": False}
    if live is not None and config is not None:
        v = live.view
        limits = config.hard_limits
        res = v.reservations
        r_risk = sum((r.risk for r in res), zero)
        r_gross = sum((r.exposure for r in res), zero)
        r_long = sum((r.exposure for r in res if r.direction is TradeDirection.LONG), zero)
        r_short = sum((r.exposure for r in res if r.direction is TradeDirection.SHORT), zero)
        r_capital = sum((r.capital for r in res), zero)
        r_margin = sum((r.margin for r in res), zero)
        e = v.equity
        available = {
            "computed": True,
            "as_of": _t(live.as_of),
            "mark_status": v.mark_status.value,
            "functional_currency": v.functional_currency,
            "equity": _s(e),
            # Loss allowance before a daily/weekly/drawdown limit, after reserved risk.
            "risk_budget": None
            if v.available_risk_budget is None
            else _s(v.available_risk_budget - r_risk),
            "risk_budget_before_reservations": _s(v.available_risk_budget),
            "per_trade_budget": _s(e * limits.risk_per_trade_pct),
            "gross_exposure": _s(limits.max_gross_exposure_pct * e - v.gross_exposure - r_gross),
            "gross_exposure_limit": _s(limits.max_gross_exposure_pct * e),
            "net_long_room": _s(limits.max_net_exposure_pct * e - (v.net_exposure + r_long)),
            "net_short_room": _s(limits.max_net_exposure_pct * e + (v.net_exposure - r_short)),
            "short_exposure": _s(limits.max_short_exposure_pct * e - v.short_exposure - r_short),
            "margin": _s(limits.max_margin_usage_pct * e - v.margin_used - r_margin),
            "capital": _s(v.available_capital - r_capital),
            "positions": limits.max_open_positions
            - len({p.asset_id for p in v.positions} | {r.asset_id for r in res}),
            "reservation_issues": v.reservation_issues,
        }
    return {
        "account_id": str(account.id),
        "reserved": reserved,
        "available": available,
        "counts": counts(session, account),
        "reconciliation": None if recon is None else _reconciliation(recon),
        "approvals_blocked": recon is not None and recon.status.value == "FAILED",
        "note": NOTE,
    }


__all__ = [
    "NOTE",
    "capacity",
    "counts",
    "events",
    "reconciliations",
    "reservation",
    "reservations",
]

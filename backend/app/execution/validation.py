"""Order authorization, validation and pre-execution revalidation (Phase 7A).

Three layers, all inside the submission transaction (account row locked):

1. **Authorization** (:func:`authorize`): who may submit, and what the order executes.
   Only the execution coordinator (``system:execution...``) and operators may submit;
   strategies, AI/LLM components and anything else are refused. An ENTRY order must
   execute an APPROVED Phase 6C reservation of this account with no order attached, not
   expired, decided by a PASS_FOR_RESERVATION assessment with its approval record, and
   whose originating strategy proposal or AI decision is still valid. An EXIT order must
   reduce the filled position of an ENTRY order of this account.
2. **Order validation**: asset and instrument specification, side, quantity (never above
   the approval, on the lot grid), price fields per order type, tick grid, the entry price
   protection (no limit or stop beyond the reserved worst entry price), expiry.
3. **Revalidation** (:func:`revalidate_entry`), the moment before the order is approved:
   no circuit breaker tripped, reservation approvals not blocked by a failed
   reconciliation, the reservation still active and unexpired. Exits are not blocked by
   breakers (halting new entries must not prevent reducing a position, spec 24.2).

A failure in 1 or 2 refuses the request (no order exists; the reservation is untouched,
so an unauthorized caller can never release someone's capacity). A failure in 3 records
the order as REJECTED and releases the reservation it never attached.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import (
    AIDecisionRecord,
    Asset,
    PortfolioAccount,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
)
from app.database.models.accounting import AssetQuantityRecord
from app.database.models.asset import AssetStatus
from app.database.models.execution import OrderRecord
from app.database.models.portfolio_account import PortfolioAccountStatus
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationRecord,
)
from app.database.models.risk import RiskAssessmentRecord
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    LIMIT_PRICE_TYPES,
    STOP_PRICE_TYPES,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.execution.models import ExecutionConfig, OrderRequest, plain
from app.risk.data import instrument_data
from app.risk.enums import (
    AssessmentResult,
    BreakerStatus,
    ExecutionLink,
    ProposalSource,
    ReservationStatus,
    TradeDirection,
)
from app.risk.reservations.service import approvals_blocked
from app.risk.sizing import sizer_for
from app.risk.state import breaker_statuses

# The execution coordinator and operators. Strategy workers, the AI decision engine, LLM
# components and ML models have no submitter identity at all.
SUBMITTER = re.compile(r"^(operator:[A-Za-z0-9._@-]+|system:execution(-[a-z0-9]+)*)$")
FORBIDDEN_WORDS = frozenset({"strategy", "strategies", "llm", "ai", "ml", "model", "decision"})
PRIORITIZED = "PRIORITIZE_FOR_REVIEW"
SUPERSEDED = "SUPERSEDED"
ENTRY_SIDE = {TradeDirection.LONG: OrderSide.BUY, TradeDirection.SHORT: OrderSide.SELL_SHORT}
EXIT_SIDE = {TradeDirection.LONG: OrderSide.SELL, TradeDirection.SHORT: OrderSide.BUY_TO_COVER}


def reason(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **{k: str(v) for k, v in extra.items()}}


@dataclass(frozen=True)
class ValidatedOrder:
    """Everything an order row needs, resolved from the authoritative records."""

    purpose: OrderPurpose
    reservation: RiskReservationRecord
    assessment_id: uuid.UUID
    asset: Asset
    spec_id: int
    quantity_increment: Decimal
    price_increment: Decimal | None
    side: OrderSide
    direction: TradeDirection
    protection_price: Decimal | None
    parent: OrderRecord | None
    expires_at: datetime


def authorize_submitter(actor: str) -> list[dict[str, Any]]:
    who = (actor or "").strip()
    tokens = set(re.split(r"[-:._@]", who.lower())) if who.startswith("system:") else set()
    if not SUBMITTER.match(who) or tokens & FORBIDDEN_WORDS:
        return [
            reason(
                "UNAUTHORIZED_SUBMITTER",
                f"{who or '(none)'} may not submit paper orders: only the execution "
                "coordinator (system:execution...) or an operator may; strategies and AI/LLM "
                "components never submit orders",
            )
        ]
    return []


def _on_grid(value: Decimal, step: Decimal) -> bool:
    return step > 0 and value % step == 0


def _expiry(
    request: OrderRequest, at: datetime, config: ExecutionConfig, out: list[dict[str, Any]]
) -> datetime:
    expires_at = request.expires_at or at + config.default_order_lifetime
    if expires_at.tzinfo is None:
        out.append(reason("EXPIRY_INVALID", "the expiration must be a UTC timestamp"))
        return at + config.default_order_lifetime
    if expires_at <= at:
        out.append(reason("EXPIRY_INVALID", f"the expiration {expires_at.isoformat()} has passed"))
    elif expires_at - at > config.max_order_lifetime:
        out.append(
            reason(
                "EXPIRY_INVALID",
                f"an order may live at most {config.max_order_lifetime} (expiration "
                f"{expires_at.isoformat()})",
            )
        )
    return expires_at


def _price_fields(request: OrderRequest, out: list[dict[str, Any]]) -> None:
    t = request.order_type
    if (request.limit_price is not None) != (t in LIMIT_PRICE_TYPES):
        out.append(
            reason(
                "PRICE_FIELDS",
                f"{t.value} orders "
                + ("need a limit price" if t in LIMIT_PRICE_TYPES else "take no limit price"),
            )
        )
    if (request.stop_price is not None) != (t in STOP_PRICE_TYPES):
        out.append(
            reason(
                "PRICE_FIELDS",
                f"{t.value} orders "
                + ("need a stop price" if t in STOP_PRICE_TYPES else "take no stop price"),
            )
        )
    trailing = (request.trail_amount is not None, request.trail_percent is not None)
    if t is OrderType.TRAILING_STOP:
        if trailing.count(True) != 1:
            out.append(
                reason("PRICE_FIELDS", "a trailing stop needs exactly one of amount or percent")
            )
    elif any(trailing):
        out.append(reason("PRICE_FIELDS", f"{t.value} orders take no trailing offset"))
    for name in ("limit_price", "stop_price", "trail_amount"):
        value = getattr(request, name)
        if value is not None and value <= 0:
            out.append(reason("PRICE_FIELDS", f"{name} must be positive"))
    if request.trail_percent is not None and not Decimal(0) < request.trail_percent < 100:
        out.append(reason("PRICE_FIELDS", "trail_percent must be between 0 and 100"))


def _instrument(
    session: Session,
    asset: Asset,
    request: OrderRequest,
    reference_price: Decimal,
    at: datetime,
    out: list[dict[str, Any]],
) -> tuple[int | None, Decimal | None, Decimal | None]:
    """(spec id, quantity increment, tick) after checking quantity and prices."""
    data = instrument_data(session, asset, at)
    if asset.status is not AssetStatus.ACTIVE:
        out.append(reason("ASSET_NOT_ACTIVE", f"{asset.symbol} is {asset.status.value}"))
    if data.quantity_increment is None or data.spec_id is None or data.min_quantity is None:
        out.append(
            reason(
                "INSTRUMENT_SPEC_MISSING",
                f"no instrument specification for {asset.symbol}: lot size is never guessed",
            )
        )
        return None, None, None
    q = request.quantity
    if q <= 0:
        out.append(reason("QUANTITY_INVALID", "the quantity must be positive"))
    elif not _on_grid(q, data.quantity_increment):
        out.append(
            reason(
                "QUANTITY_PRECISION",
                f"{q} is not a multiple of the lot size {plain(data.quantity_increment)}",
            )
        )
    elif q < data.min_quantity:
        out.append(reason("QUANTITY_BELOW_MINIMUM", f"{q} is below {plain(data.min_quantity)}"))
    sizer = sizer_for(asset.asset_class.value)
    tick = None if sizer is None else sizer.tick_size(data.provider_tick_size, reference_price)
    priced = [
        (n, getattr(request, n))
        for n in ("limit_price", "stop_price", "trail_amount")
        if getattr(request, n) is not None
    ]
    if priced and tick is None:
        out.append(reason("TICK_SIZE_UNKNOWN", f"the price increment of {asset.symbol} is unknown"))
    elif tick is not None:
        for name, value in priced:
            if value > 0 and not _on_grid(value, tick):
                out.append(
                    reason(
                        "PRICE_PRECISION",
                        f"{name} {value} is not on the {plain(tick)} tick grid",
                    )
                )
    return data.spec_id, data.quantity_increment, tick


def _stop_limit(request: OrderRequest, side: OrderSide, out: list[dict[str, Any]]) -> None:
    if request.order_type is not OrderType.STOP_LIMIT:
        return
    if request.limit_price is None or request.stop_price is None:
        return
    bad = (
        request.limit_price < request.stop_price
        if side.buys
        else (request.limit_price > request.stop_price)
    )
    if bad:
        out.append(
            reason(
                "STOP_LIMIT_INCONSISTENT",
                "a buying stop-limit needs limit >= stop, a selling one limit <= stop",
            )
        )


def _protection(
    request: OrderRequest, side: OrderSide, protection: Decimal, out: list[dict[str, Any]]
) -> None:
    """No entry price level beyond the worst entry price the reservation was sized for."""
    for name in ("limit_price", "stop_price"):
        value = getattr(request, name)
        if value is None:
            continue
        beyond = value > protection if side.buys else value < protection
        if beyond:
            out.append(
                reason(
                    "PRICE_BEYOND_PROTECTION",
                    f"{name} {value} is beyond the reserved worst entry price {plain(protection)}: "
                    "the order would carry more risk than was reserved",
                )
            )


def _origin(
    session: Session,
    request: OrderRequest,
    r: RiskReservationRecord,
    actor: str,
    at: datetime,
    out: list[dict[str, Any]],
) -> None:
    """The originating intent is still valid and is the one the request declares."""
    for declared, recorded, label in (
        (request.strategy_proposal_id, r.strategy_proposal_id, "strategy proposal"),
        (request.ai_decision_id, r.ai_decision_id, "AI decision"),
    ):
        if declared is not None and declared != recorded:
            out.append(
                reason("ORIGIN_MISMATCH", f"the request names another {label} than the reservation")
            )
    if r.source is ProposalSource.OPERATOR and not actor.startswith("operator:"):
        out.append(
            reason(
                "OPERATOR_INTENT",
                "the reservation was requested by an operator: only an operator may execute it",
            )
        )
    if r.strategy_proposal_id is not None:
        proposal = session.get(StrategyProposalRecord, r.strategy_proposal_id)
        if proposal is None:  # pragma: no cover - foreign key
            out.append(reason("ORIGIN_INVALID", "the strategy proposal does not exist"))
        else:
            if proposal.expires_at is not None and proposal.expires_at <= at:
                out.append(
                    reason(
                        "PROPOSAL_EXPIRED",
                        f"the strategy proposal expired at {proposal.expires_at.isoformat()}",
                    )
                )
            superseded = session.scalar(
                select(StrategyProposalEventRecord.id).where(
                    StrategyProposalEventRecord.proposal_id == proposal.id,
                    StrategyProposalEventRecord.kind == SUPERSEDED,
                    StrategyProposalEventRecord.occurred_at <= at,
                )
            )
            if superseded is not None:
                out.append(reason("PROPOSAL_SUPERSEDED", "a newer strategy proposal superseded it"))
    if r.ai_decision_id is not None:
        decision = session.get(AIDecisionRecord, r.ai_decision_id)
        if decision is None:  # pragma: no cover - foreign key
            out.append(reason("ORIGIN_INVALID", "the AI decision does not exist"))
        elif decision.outcome.value != PRIORITIZED:
            out.append(reason("ORIGIN_INVALID", f"the AI decision is {decision.outcome.value}"))
        elif decision.expires_at is not None and decision.expires_at <= at:
            out.append(
                reason(
                    "DECISION_EXPIRED",
                    f"the AI decision expired at {decision.expires_at.isoformat()}",
                )
            )
    if r.source is ProposalSource.STRATEGY_PROPOSAL and r.strategy_proposal_id is None:
        out.append(reason("ORIGIN_INVALID", "the reservation lost its strategy proposal"))
    if r.source is ProposalSource.AI_DECISION and r.ai_decision_id is None:
        out.append(reason("ORIGIN_INVALID", "the reservation lost its AI decision"))


def validate_entry(
    session: Session,
    account: PortfolioAccount,
    request: OrderRequest,
    actor: str,
    at: datetime,
    config: ExecutionConfig,
) -> tuple[list[dict[str, Any]], ValidatedOrder | None, RiskReservationRecord | None]:
    out: list[dict[str, Any]] = []
    if request.parent_order_id is not None:
        out.append(reason("PARENT_NOT_ALLOWED", "an ENTRY order has no parent order"))
    if request.exit_reason is not None:
        out.append(reason("EXIT_REASON_ON_ENTRY", "an ENTRY order has no exit reason"))
    if request.order_type is OrderType.TAKE_PROFIT:
        out.append(reason("TAKE_PROFIT_IS_EXIT", "a take-profit order only reduces a position"))
    if request.reservation_id is None:
        out.append(reason("RESERVATION_REQUIRED", "an ENTRY order must name its reservation"))
        return out, None, None
    r = session.scalar(
        select(RiskReservationRecord)
        .where(RiskReservationRecord.id == request.reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if r is None:
        out.append(
            reason("RESERVATION_NOT_FOUND", f"reservation {request.reservation_id} does not exist")
        )
        return out, None, None
    if r.portfolio_account_id != account.id:
        out.append(
            reason("RESERVATION_OTHER_ACCOUNT", "the reservation belongs to another account")
        )
        return out, None, None
    existing = session.scalar(
        select(OrderRecord.id).where(
            OrderRecord.reservation_id == r.id, OrderRecord.purpose == OrderPurpose.ENTRY
        )
    )
    if existing is not None:
        out.append(
            reason(
                "DUPLICATE_ORDER",
                f"order {existing} already executes this reservation (one order per "
                "reservation; a retry must reuse its idempotency key)",
            )
        )
    if r.status is not ReservationStatus.APPROVED:
        out.append(
            reason("RESERVATION_NOT_ACTIVE", f"the reservation is {r.status.value}, not APPROVED")
        )
    elif r.execution_link is not ExecutionLink.NONE and existing is None:
        out.append(
            reason(
                "RESERVATION_LINKED",
                f"the reservation is {r.execution_link.value}: another execution depends on it",
            )
        )
    if r.expires_at <= at:
        out.append(
            reason("RESERVATION_EXPIRED", f"the reservation expired at {r.expires_at.isoformat()}")
        )
    assessment = (
        None if r.assessment_id is None else session.get(RiskAssessmentRecord, r.assessment_id)
    )
    if assessment is None or assessment.result is not AssessmentResult.PASS_FOR_RESERVATION:
        out.append(
            reason("ASSESSMENT_NOT_APPROVED", "the reservation has no passing risk assessment")
        )
    approval = session.scalar(
        select(RiskReservationApprovalRecord).where(
            RiskReservationApprovalRecord.reservation_id == r.id
        )
    )
    if approval is None:
        out.append(reason("APPROVAL_MISSING", "the reservation has no approved order intent"))
    if out:
        return out, None, r
    assert approval is not None  # noqa: S101  # checked above
    assert assessment is not None  # noqa: S101  # checked above
    _origin(session, request, r, actor, at, out)
    asset = session.get(Asset, r.asset_id)
    assert asset is not None  # noqa: S101  # foreign key
    if request.asset_id is not None and request.asset_id != r.asset_id:
        out.append(reason("ASSET_MISMATCH", "the request names another asset than the reservation"))
    side = ENTRY_SIDE[r.direction]
    if request.side is not None and request.side is not side:
        out.append(
            reason(
                "SIDE_MISMATCH",
                f"a {r.direction.value} reservation is entered with {side.value}, not "
                f"{request.side.value}",
            )
        )
    conflict = opposite_position(session, account.id, r.asset_id, r.direction)
    if conflict is not None:
        out.append(conflict)
    if request.quantity > approval.approved_quantity:
        out.append(
            reason(
                "QUANTITY_ABOVE_APPROVAL",
                f"{request.quantity} exceeds the approved {plain(approval.approved_quantity)}",
            )
        )
    _price_fields(request, out)
    spec_id, increment, tick = _instrument(session, asset, request, r.entry, at, out)
    _stop_limit(request, side, out)
    _protection(request, side, approval.worst_entry_price, out)
    expires_at = _expiry(request, at, config, out)
    if out or spec_id is None or increment is None:
        return out, None, r
    return (
        out,
        ValidatedOrder(
            purpose=OrderPurpose.ENTRY,
            reservation=r,
            assessment_id=assessment.id,
            asset=asset,
            spec_id=spec_id,
            quantity_increment=increment,
            price_increment=tick,
            side=side,
            direction=r.direction,
            protection_price=approval.worst_entry_price,
            parent=None,
            expires_at=expires_at,
        ),
        r,
    )


def position_quantity(session: Session, account_id: uuid.UUID, asset_id: uuid.UUID) -> Decimal:
    """The signed ledger position (6A asset quantity subledger)."""
    total = session.scalar(
        select(func.coalesce(func.sum(AssetQuantityRecord.quantity_delta), 0)).where(
            AssetQuantityRecord.portfolio_account_id == account_id,
            AssetQuantityRecord.asset_id == asset_id,
        )
    )
    return Decimal(total or 0)


def opposite_position(
    session: Session, account_id: uuid.UUID, asset_id: uuid.UUID, direction: TradeDirection
) -> dict[str, Any] | None:
    """Phase 7B: the 6A ledger cannot hold a long and a short of one asset at once, so an
    entry against an open position of the other direction is refused (close it first)."""
    held = position_quantity(session, account_id, asset_id)
    if (direction is TradeDirection.LONG and held < 0) or (
        direction is TradeDirection.SHORT and held > 0
    ):
        return reason(
            "OPPOSITE_POSITION",
            f"the account holds {plain(held)} of this asset: a {direction.value} entry would "
            "need the opposite position closed first",
        )
    return None


def closable_quantity(session: Session, parent: OrderRecord) -> Decimal:
    """What exits of ``parent`` may still close: its filled quantity minus what its exits
    filled and what live exits may still fill."""
    exits = session.scalars(
        select(OrderRecord).where(
            OrderRecord.parent_order_id == parent.id, OrderRecord.purpose == OrderPurpose.EXIT
        )
    ).all()
    committed = sum(
        (
            e.filled_quantity
            + (Decimal(0) if e.status in FINAL_ORDER_STATUSES else e.remaining_quantity)
            for e in exits
        ),
        Decimal(0),
    )
    return parent.filled_quantity - committed


def validate_exit(
    session: Session,
    account: PortfolioAccount,
    request: OrderRequest,
    actor: str,
    at: datetime,
    config: ExecutionConfig,
) -> tuple[list[dict[str, Any]], ValidatedOrder | None, RiskReservationRecord | None]:
    out: list[dict[str, Any]] = []
    if request.parent_order_id is None:
        out.append(reason("PARENT_REQUIRED", "an EXIT order must name the ENTRY order it reduces"))
        return out, None, None
    parent = session.scalar(
        select(OrderRecord)
        .where(OrderRecord.id == request.parent_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        out.append(reason("PARENT_NOT_FOUND", f"order {request.parent_order_id} does not exist"))
        return out, None, None
    if parent.portfolio_account_id != account.id:
        out.append(reason("PARENT_OTHER_ACCOUNT", "the parent order belongs to another account"))
        return out, None, None
    if parent.purpose is not OrderPurpose.ENTRY:
        out.append(reason("PARENT_NOT_ENTRY", "an exit reduces the position of an ENTRY order"))
        return out, None, None
    r = session.get(RiskReservationRecord, parent.reservation_id)
    assert r is not None  # noqa: S101  # foreign key
    if request.reservation_id is not None and request.reservation_id != parent.reservation_id:
        out.append(
            reason("RESERVATION_MISMATCH", "an exit is linked to its parent's reservation only")
        )
    if request.asset_id is not None and request.asset_id != parent.asset_id:
        out.append(reason("ASSET_MISMATCH", "the request names another asset than the parent"))
    side = EXIT_SIDE[parent.direction]
    if request.side is not None and request.side is not side:
        out.append(
            reason(
                "SIDE_MISMATCH",
                f"a {parent.direction.value} position is reduced with {side.value}, not "
                f"{request.side.value}",
            )
        )
    if parent.filled_quantity <= 0:
        out.append(reason("NOTHING_TO_CLOSE", "the parent order has not filled anything"))
    else:
        closable = closable_quantity(session, parent)
        held = position_quantity(session, account.id, parent.asset_id)
        held = held if parent.direction is TradeDirection.LONG else -held
        limit = min(closable, max(held, Decimal(0)))
        if request.quantity > limit:
            out.append(
                reason(
                    "QUANTITY_ABOVE_POSITION",
                    f"{request.quantity} exceeds the {plain(limit)} the exit may close "
                    f"(parent filled {plain(parent.filled_quantity)}, ledger holds "
                    f"{plain(held)})",
                )
            )
    asset = session.get(Asset, parent.asset_id)
    assert asset is not None  # noqa: S101  # foreign key
    _price_fields(request, out)
    reference = parent.average_fill_price or r.entry
    spec_id, increment, tick = _instrument(session, asset, request, reference, at, out)
    _stop_limit(request, side, out)
    expires_at = _expiry(request, at, config, out)
    if out or spec_id is None or increment is None:
        return out, None, r
    return (
        out,
        ValidatedOrder(
            purpose=OrderPurpose.EXIT,
            reservation=r,
            assessment_id=parent.risk_assessment_id,
            asset=asset,
            spec_id=spec_id,
            quantity_increment=increment,
            price_increment=tick,
            side=side,
            direction=parent.direction,
            protection_price=None,
            parent=parent,
            expires_at=expires_at,
        ),
        r,
    )


def revalidate_entry(
    session: Session, account: PortfolioAccount, r: RiskReservationRecord, at: datetime
) -> list[dict[str, Any]]:
    """The risk conditions that must still hold the moment an entry order is approved."""
    out: list[dict[str, Any]] = []
    if account.status is not PortfolioAccountStatus.ACTIVE:
        out.append(reason("ACCOUNT_NOT_ACTIVE", f"the account is {account.status.value}"))
    tripped = sorted(
        b.value
        for b, s in breaker_statuses(session, account.id).items()
        if s is BreakerStatus.TRIPPED
    )
    if tripped:
        out.append(
            reason(
                "RISK_HALTED",
                "new entries are halted: circuit breaker(s) tripped: " + ", ".join(tripped),
            )
        )
    blocked = approvals_blocked(session, account.id)
    if blocked is not None:
        out.append(
            reason(
                "RESERVATION_RECONCILIATION_FAILED",
                f"reservation reconciliation {blocked.id} FAILED: no new execution",
            )
        )
    if r.status is not ReservationStatus.APPROVED or r.expires_at <= at:
        out.append(reason("RESERVATION_NOT_ACTIVE", "the reservation is no longer active"))
    return out


def check_fill(order: OrderRecord, quantity: Decimal, price: Decimal) -> list[dict[str, Any]]:
    """A fill must respect the order: quantity on the grid and within what remains, price on
    the tick grid, within the limit (limit-type orders) and within the entry protection.
    Phase 7B's fill model must produce only fills that pass; Phase 7A refuses controlled
    test events that do not."""
    out: list[dict[str, Any]] = []
    if order.status not in (
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    ):
        out.append(reason("NOT_FILLABLE", f"the order is {order.status.value}"))
    if quantity <= 0 or quantity > order.remaining_quantity:
        out.append(
            reason(
                "OVER_FILL",
                f"{plain(quantity)} does not fit the {plain(order.remaining_quantity)} remaining",
            )
        )
    elif not _on_grid(quantity, order.quantity_increment):
        out.append(reason("QUANTITY_PRECISION", f"{quantity} is not on the lot grid"))
    if price <= 0:
        out.append(reason("PRICE_INVALID", "a fill price must be positive"))
        return out
    if order.price_increment is not None and not _on_grid(price, order.price_increment):
        out.append(reason("PRICE_PRECISION", f"{price} is not on the tick grid"))
    if order.limit_price is not None:
        beyond = price > order.limit_price if order.side.buys else price < order.limit_price
        if beyond:
            out.append(
                reason("LIMIT_VIOLATED", f"{price} is beyond the limit {plain(order.limit_price)}")
            )
    if order.protection_price is not None:
        beyond = (
            price > order.protection_price if order.side.buys else price < order.protection_price
        )
        if beyond:
            out.append(
                reason(
                    "PROTECTION_VIOLATED",
                    f"{price} is beyond the reserved worst entry price "
                    f"{plain(order.protection_price)}",
                )
            )
    return out


__all__ = [
    "ENTRY_SIDE",
    "EXIT_SIDE",
    "SUBMITTER",
    "ValidatedOrder",
    "authorize_submitter",
    "check_fill",
    "closable_quantity",
    "opposite_position",
    "position_quantity",
    "reason",
    "revalidate_entry",
    "validate_entry",
    "validate_exit",
]

"""Simulated positions: the trade each ENTRY order opens (Phase 7B).

**Simulated positions of a paper account.** A position is opened by the first fill of an
ENTRY order and reduced by the fills of that order's EXIT orders; it is closed when nothing
is open and the entry order is final (no further entry fill can come). Every change happens
inside the transaction that applies the fill or order event, under the account lock, and is
recorded as a ``position_events`` row (append-only, gapless).

The figures are not a second accounting system. Quantities and prices come from the fills
(each tied to exactly one 6A journal entry); **realized P/L is read from the 6A asset
quantity ledger movement of each exit fill** (average cost, as the ledger computes it), and
commissions and fees are the amounts each fill posted. When two positions in the same asset
are open at once, the ledger's average cost blends them, so their realized P/L is the
ledger's, not a lot-by-lot figure (documented limitation).

Protection is recorded at opening from the authoritative records: the stop and target of the
6C approval the reservation was sized with (the stop may later only tighten), and, for
strategy-originated reservations, the proposal's invalidation level and holding horizon
(``holding_horizon_periods`` x its timeframe = the position's expiry).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models.accounting import AssetQuantityRecord
from app.database.models.execution import (
    OrderFillRecord,
    OrderRecord,
    PaperPositionRecord,
    PositionEventRecord,
)
from app.database.models.reservations import RiskReservationApprovalRecord, RiskReservationRecord
from app.database.models.strategy import StrategyProposalRecord
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    ExitReason,
    OrderPurpose,
    OrderStatus,
    PositionEventType,
    PositionStatus,
)
from app.execution.models import plain
from app.paper_broker.market import timeframe_length

logger = get_logger(__name__)
ENGINE_ACTOR = "system:execution-engine"
ZERO = Decimal(0)
PRICE_PLACES = Decimal("1e-10")


def _avg(value: Decimal, quantity: Decimal) -> Decimal:
    return (value / quantity).quantize(PRICE_PLACES)


class PositionBook:
    """Opens, reduces and closes positions from applied fills and final orders."""

    # ------------------------------------------------------------------ reads
    @staticmethod
    def for_entry(
        session: Session, entry_order_id: uuid.UUID, *, lock: bool = True
    ) -> PaperPositionRecord | None:
        query = select(PaperPositionRecord).where(
            PaperPositionRecord.entry_order_id == entry_order_id
        )
        if lock:
            query = query.with_for_update()
        return session.scalar(query.execution_options(populate_existing=True))

    @staticmethod
    def event(
        session: Session,
        position: PaperPositionRecord,
        event_type: PositionEventType,
        *,
        at: datetime,
        actor: str,
        reason: str,
        quantity: Decimal = ZERO,
        price: Decimal | None = None,
        order_id: uuid.UUID | None = None,
        fill_id: uuid.UUID | None = None,
        journal_entry_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> PositionEventRecord:
        position.last_event_sequence += 1
        session.flush()  # the event guard checks its number and quantity against the row
        record = PositionEventRecord(
            position_id=position.id,
            sequence=position.last_event_sequence,
            event_type=event_type,
            occurred_at=at,
            actor=actor,
            reason=reason[:2000],
            quantity=quantity,
            price=price,
            open_quantity_after=position.open_quantity,
            order_id=order_id,
            fill_id=fill_id,
            journal_entry_id=journal_entry_id,
            details=details or {},
        )
        session.add(record)
        session.flush()
        logger.info(
            "execution.position_event",
            simulated=True,
            position_id=str(position.id),
            event_type=event_type.value,
        )
        return record

    # ------------------------------------------------------------------ fills
    def on_fill(
        self,
        session: Session,
        order: OrderRecord,
        fill: OrderFillRecord,
        *,
        at: datetime,
        actor: str = ENGINE_ACTOR,
    ) -> PaperPositionRecord | None:
        if order.purpose is OrderPurpose.ENTRY:
            return self._entry_fill(session, order, fill, at, actor)
        return self._exit_fill(session, order, fill, at, actor)

    def _open(
        self, session: Session, order: OrderRecord, fill: OrderFillRecord, at: datetime
    ) -> PaperPositionRecord:
        r = session.get(RiskReservationRecord, order.reservation_id)
        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == order.reservation_id
            )
        )
        assert r is not None  # noqa: S101  # foreign key
        assert approval is not None  # noqa: S101  # an entry order needs an approval (0016)
        proposal = (
            None
            if r.strategy_proposal_id is None
            else session.get(StrategyProposalRecord, r.strategy_proposal_id)
        )
        expires_at = None
        horizon_note = "no holding horizon: the position does not expire by itself"
        if proposal is not None:
            try:
                expires_at = at + proposal.holding_horizon_periods * timeframe_length(
                    proposal.timeframe
                )
                horizon_note = (
                    f"expires after {proposal.holding_horizon_periods} x {proposal.timeframe}"
                )
            except ValueError:
                horizon_note = f"timeframe {proposal.timeframe} has no fixed length: no expiry"
        position = PaperPositionRecord(
            id=uuid.uuid4(),
            portfolio_account_id=order.portfolio_account_id,
            asset_id=order.asset_id,
            symbol=order.symbol,
            asset_class=order.asset_class,
            currency=order.quote_currency,
            direction=order.direction,
            status=PositionStatus.OPEN,
            entry_order_id=order.id,
            reservation_id=order.reservation_id,
            strategy_proposal_id=r.strategy_proposal_id,
            ai_decision_id=r.ai_decision_id,
            strategy_id=None if proposal is None else proposal.strategy_id,
            origin=r.source.value,
            quantity_increment=order.quantity_increment,
            price_increment=order.price_increment,
            quantity_opened=fill.quantity,
            quantity_closed=ZERO,
            open_quantity=fill.quantity,
            entry_value=fill.quantity * fill.price,
            average_entry_price=fill.price,
            commissions=fill.commission,
            fees=fill.fees,
            initial_stop_price=approval.stop,
            stop_price=approval.stop,
            target_price=approval.target,
            invalidation_price=None if proposal is None else proposal.invalidation_level,
            expires_at=expires_at,
            opened_at=at,
        )
        session.add(position)
        session.flush()
        self.event(
            session,
            position,
            PositionEventType.OPENED,
            at=at,
            actor=ENGINE_ACTOR,
            reason=f"{position.direction.value} {plain(fill.quantity)} {position.symbol} opened "
            f"at {plain(fill.price)} (simulated fill)",
            quantity=fill.quantity,
            price=fill.price,
            order_id=order.id,
            fill_id=fill.id,
            journal_entry_id=fill.journal_entry_id,
        )
        self.event(
            session,
            position,
            PositionEventType.PROTECTION_SET,
            at=at,
            actor=ENGINE_ACTOR,
            reason=f"stop {plain(approval.stop)} (approved), target "
            f"{'-' if approval.target is None else plain(approval.target)}; {horizon_note}",
            details={
                "stop": str(approval.stop),
                "target": None if approval.target is None else str(approval.target),
                "invalidation": None
                if position.invalidation_price is None
                else str(position.invalidation_price),
                "expires_at": None if expires_at is None else expires_at.isoformat(),
                "approval_id": str(approval.id),
                "strategy_proposal_id": None if proposal is None else str(proposal.id),
            },
        )
        return position

    def _entry_fill(
        self,
        session: Session,
        order: OrderRecord,
        fill: OrderFillRecord,
        at: datetime,
        actor: str,
    ) -> PaperPositionRecord:
        position = self.for_entry(session, order.id)
        if position is None:
            return self._open(session, order, fill, at)
        position.quantity_opened += fill.quantity
        position.open_quantity += fill.quantity
        position.entry_value += fill.quantity * fill.price
        position.average_entry_price = _avg(position.entry_value, position.quantity_opened)
        position.commissions += fill.commission
        position.fees += fill.fees
        self.event(
            session,
            position,
            PositionEventType.INCREASED,
            at=at,
            actor=actor,
            reason=f"+{plain(fill.quantity)} at {plain(fill.price)} (entry fill)",
            quantity=fill.quantity,
            price=fill.price,
            order_id=order.id,
            fill_id=fill.id,
            journal_entry_id=fill.journal_entry_id,
        )
        return position

    def _exit_fill(
        self,
        session: Session,
        order: OrderRecord,
        fill: OrderFillRecord,
        at: datetime,
        actor: str,
    ) -> PaperPositionRecord | None:
        assert order.parent_order_id is not None  # noqa: S101  # database CHECK
        position = self.for_entry(session, order.parent_order_id)
        if position is None:  # a 7A exit of an entry filled before positions existed
            logger.warning("execution.exit_without_position", order_id=str(order.id))
            return None
        if fill.quantity > position.open_quantity:
            raise ValueError(
                f"exit fill {fill.id} of {plain(fill.quantity)} exceeds the "
                f"{plain(position.open_quantity)} open in position {position.id}"
            )
        realized = Decimal(
            session.scalar(
                select(func.coalesce(func.sum(AssetQuantityRecord.realized_pnl), 0)).where(
                    AssetQuantityRecord.entry_id == fill.journal_entry_id,
                    AssetQuantityRecord.asset_id == order.asset_id,
                )
            )
            or 0
        )
        position.quantity_closed += fill.quantity
        position.open_quantity -= fill.quantity
        position.exit_value += fill.quantity * fill.price
        position.average_exit_price = _avg(position.exit_value, position.quantity_closed)
        position.realized_pnl += realized
        position.commissions += fill.commission
        position.fees += fill.fees
        position.exit_reason = order.exit_reason or ExitReason.MANUAL_EXIT
        self.event(
            session,
            position,
            PositionEventType.REDUCED,
            at=at,
            actor=actor,
            reason=f"-{plain(fill.quantity)} at {plain(fill.price)} "
            f"({position.exit_reason.value}); realized {plain(realized)} {position.currency}",
            quantity=fill.quantity,
            price=fill.price,
            order_id=order.id,
            fill_id=fill.id,
            journal_entry_id=fill.journal_entry_id,
            details={"realized_pnl": str(realized), "exit_reason": position.exit_reason.value},
        )
        # A fill that completes its exit order closes the position after the order's end is
        # recorded (on_order_final); a partial exit fill may close it now.
        if fill.quantity < order.remaining_quantity:
            self.maybe_close(session, position, at=at, actor=actor)
        return position

    # ------------------------------------------------------------------ order outcomes
    def on_order_final(
        self, session: Session, order: OrderRecord, *, at: datetime, actor: str = ENGINE_ACTOR
    ) -> None:
        if order.purpose is OrderPurpose.ENTRY:
            position = self.for_entry(session, order.id)
            if position is not None:
                self.maybe_close(session, position, at=at, actor=actor)
            return
        assert order.parent_order_id is not None  # noqa: S101  # database CHECK
        position = self.for_entry(session, order.parent_order_id)
        if position is None:
            return
        self.event(
            session,
            position,
            PositionEventType.EXIT_ENDED,
            at=at,
            actor=actor,
            reason=f"exit {order.order_type.value} {order.status.value}: "
            f"{plain(order.filled_quantity)} of {plain(order.quantity)} filled",
            order_id=order.id,
            details={
                "status": order.status.value,
                "exit_reason": None if order.exit_reason is None else order.exit_reason.value,
            },
        )
        self.maybe_close(session, position, at=at, actor=actor)

    def maybe_close(
        self, session: Session, position: PaperPositionRecord, *, at: datetime, actor: str
    ) -> bool:
        if position.status is PositionStatus.CLOSED or position.open_quantity != ZERO:
            return False
        entry = session.get(OrderRecord, position.entry_order_id)
        assert entry is not None  # noqa: S101  # foreign key
        if entry.status not in FINAL_ORDER_STATUSES:
            return False  # an entry fill may still come (e.g. a pending cancellation)
        position.status = PositionStatus.CLOSED
        position.closed_at = at
        position.unrealized_pnl = ZERO
        net = position.realized_pnl - position.commissions - position.fees - position.borrow_accrued
        self.event(
            session,
            position,
            PositionEventType.CLOSED,
            at=at,
            actor=actor,
            reason=f"closed ({position.exit_reason.value if position.exit_reason else '-'}): "
            f"realized {plain(position.realized_pnl)}, costs "
            f"{plain(position.commissions + position.fees + position.borrow_accrued)}, net "
            f"{plain(net)} {position.currency} (simulated)",
            details={
                "realized_pnl": str(position.realized_pnl),
                "net_pnl": str(net),
                "entry_status": entry.status.value,
            },
        )
        return True


def is_live(order: OrderRecord) -> bool:
    return order.status not in FINAL_ORDER_STATUSES


def entry_live(order: OrderRecord) -> bool:
    return order.status in (
        OrderStatus.APPROVED,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    )


__all__ = ["ENGINE_ACTOR", "PositionBook", "entry_live", "is_live"]

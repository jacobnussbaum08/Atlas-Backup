"""The controlled paper broker: Atlas's isolated, simulated broker (Phases 7A-7B).

**Simulated. No real brokerage.** The simulator keeps its own book in PostgreSQL
(``paper_broker_orders``) and answers with events (``paper_broker_events``), each written
together with an outbox event in the caller's transaction.

Fills come from two labelled sources only:

* SIMULATION (Phase 7B): :meth:`record_simulated_fill` / :meth:`record_trigger`, called by
  the execution simulator (``app.execution.simulation``) with a decision of the fill model
  (``app.paper_broker.fill_model``) under a recorded execution-model version;
* CONTROLLED_TEST: :meth:`inject_fill` / :meth:`confirm_cancel` (MANUAL cancel mode), test
  or operator events with an idempotency key, on CONTROLLED_TEST accounts only.

What it does decide, deterministically:

* acknowledgement (ACCEPTED) of an approved order, or REJECTED when it expired before
  receipt; a duplicate submission returns the original answer and records nothing;
* cancellation: CANCEL_CONFIRMED while the order is working (IMMEDIATE mode), a tombstone
  CANCEL_CONFIRMED when it never received the order, CANCEL_REJECTED when it is already
  filled;
* expiry of a working order once its expiry time has passed.

Its sequence counter is per broker order (no global serialization point across accounts).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models.execution import (
    PAPER_BROKER_ID,
    OrderRecord,
    PaperBrokerEventRecord,
    PaperBrokerOrderRecord,
    PaperBrokerStateRecord,
)
from app.events.outbox import enqueue
from app.execution.enums import (
    FINAL_BROKER_STATUSES,
    BrokerEventSource,
    BrokerEventType,
    BrokerOrderStatus,
    BrokerRunState,
    CancelMode,
    OrderStatus,
)
from app.paper_broker.interface import (
    BrokerHealth,
    BrokerOrderView,
    BrokerRecovery,
    BrokerRefusedError,
    BrokerUnavailableError,
)

logger = get_logger(__name__)
# Stored in paper_broker_state.fill_model (64 characters at most).
FILL_MODEL = "SIMULATED from stored market data (versions: execution_models)"
EVENT_TOPIC = "paper_broker.event"
ZERO = Decimal(0)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


class ControlledPaperBroker:
    """The simulated paper broker. ``cancel_mode`` MANUAL lets a test hold a cancellation
    pending (and fill meanwhile) before confirming it."""

    def __init__(
        self, broker_id: str = PAPER_BROKER_ID, *, cancel_mode: CancelMode = CancelMode.IMMEDIATE
    ) -> None:
        self.broker_id = broker_id
        self.cancel_mode = cancel_mode

    # ---------------------------------------------------------------- state
    def _state(self, session: Session) -> PaperBrokerStateRecord:
        session.execute(
            pg_insert(PaperBrokerStateRecord)
            .values(
                broker_id=self.broker_id,
                status=BrokerRunState.STOPPED,
                fill_model=FILL_MODEL,
                details={"cancel_mode": self.cancel_mode.value},
            )
            .on_conflict_do_nothing(index_elements=["broker_id"])
        )
        state = session.get(PaperBrokerStateRecord, self.broker_id, populate_existing=True)
        assert state is not None  # noqa: S101  # inserted above
        return state

    def _require_running(self, session: Session) -> None:
        state = session.get(PaperBrokerStateRecord, self.broker_id, populate_existing=True)
        if state is None:
            raise BrokerUnavailableError("the paper broker has not been started")
        if state.status is not BrokerRunState.RUNNING:
            raise BrokerUnavailableError(
                f"the paper broker is {state.status.value}: retry when it is RUNNING"
            )

    def _book(self, session: Session, client_order_id: uuid.UUID) -> PaperBrokerOrderRecord | None:
        return session.scalar(
            select(PaperBrokerOrderRecord)
            .where(PaperBrokerOrderRecord.client_order_id == client_order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _new_book(
        self, session: Session, order: OrderRecord, at: datetime, status: BrokerOrderStatus
    ) -> PaperBrokerOrderRecord:
        book = PaperBrokerOrderRecord(
            id=uuid.uuid4(),
            broker_id=self.broker_id,
            client_order_id=order.id,
            portfolio_account_id=order.portfolio_account_id,
            asset_id=order.asset_id,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            filled_quantity=ZERO,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            status=status,
            expires_at=order.expires_at,
            received_at=at,
            last_event_sequence=0,
        )
        session.add(book)
        session.flush()
        return book

    def _event(
        self,
        session: Session,
        book: PaperBrokerOrderRecord,
        event_type: BrokerEventType,
        at: datetime,
        reason: str,
        *,
        source: BrokerEventSource = BrokerEventSource.SYSTEM,
        quantity: Decimal = ZERO,
        price: Decimal | None = None,
        commission: Decimal = ZERO,
        fees: Decimal = ZERO,
        external_ref: str | None = None,
        details: dict[str, Any] | None = None,
        execution_model_id: int | None = None,
    ) -> PaperBrokerEventRecord:
        book.last_event_sequence += 1
        session.flush()  # the event guard checks its number against the book row
        event = PaperBrokerEventRecord(
            id=uuid.uuid4(),
            broker_id=self.broker_id,
            broker_order_id=book.id,
            client_order_id=book.client_order_id,
            sequence=book.last_event_sequence,
            event_type=event_type,
            source=source,
            quantity=quantity,
            price=price,
            commission=commission,
            fees=fees,
            external_ref=external_ref,
            execution_model_id=execution_model_id,
            reason=reason,
            occurred_at=at,
            details=details or {},
        )
        session.add(event)
        session.flush()
        enqueue(
            session,
            topic=EVENT_TOPIC,
            aggregate_type="order",
            aggregate_id=book.client_order_id,
            dedupe_key=f"{EVENT_TOPIC}:{event.id}",
            payload={
                "broker_event_id": str(event.id),
                "client_order_id": str(book.client_order_id),
                "sequence": event.sequence,
                "event_type": event_type.value,
            },
            at=at,
        )
        logger.info(
            "paper_broker.event",
            simulated=True,
            client_order_id=str(book.client_order_id),
            sequence=event.sequence,
            event_type=event_type.value,
            source=source.value,
        )
        return event

    # ---------------------------------------------------------------- orders
    def submit_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord:
        self._require_running(session)
        book = self._book(session, order.id)
        if book is not None:
            first = session.scalar(
                select(PaperBrokerEventRecord).where(
                    PaperBrokerEventRecord.broker_order_id == book.id,
                    PaperBrokerEventRecord.sequence == 1,
                )
            )
            assert first is not None  # noqa: S101  # a book row is created with its event
            return first
        if order.status is not OrderStatus.APPROVED:
            raise BrokerRefusedError(
                f"order {order.id} is {order.status.value}: only an APPROVED order is submitted"
            )
        if order.expires_at <= at:
            book = self._new_book(session, order, at, BrokerOrderStatus.REJECTED)
            return self._event(
                session,
                book,
                BrokerEventType.REJECTED,
                at,
                f"expired at {order.expires_at.isoformat()} before the simulator received it",
            )
        book = self._new_book(session, order, at, BrokerOrderStatus.WORKING)
        return self._event(
            session, book, BrokerEventType.ACCEPTED, at, "accepted by the simulated paper broker"
        )

    def cancel_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord | None:
        self._require_running(session)
        book = self._book(session, order.id)
        if book is None:
            # Never received (the submission is still queued): a tombstone, so a late
            # submission of this client order is answered with the cancellation.
            book = self._new_book(session, order, at, BrokerOrderStatus.CANCELLED)
            return self._event(
                session,
                book,
                BrokerEventType.CANCEL_CONFIRMED,
                at,
                "cancelled before the simulated paper broker received the order",
            )
        if book.status is BrokerOrderStatus.FILLED:
            return self._event(
                session, book, BrokerEventType.CANCEL_REJECTED, at, "already completely filled"
            )
        if book.status in FINAL_BROKER_STATUSES:
            return None  # its final event is already recorded and will be applied
        if self.cancel_mode is CancelMode.MANUAL:
            return None
        book.status = BrokerOrderStatus.CANCELLED
        return self._event(
            session,
            book,
            BrokerEventType.CANCEL_CONFIRMED,
            at,
            f"cancelled by the simulated paper broker ({_plain(book.filled_quantity)} of "
            f"{_plain(book.quantity)} filled)",
        )

    def confirm_cancel(
        self, session: Session, client_order_id: uuid.UUID, at: datetime, *, external_ref: str
    ) -> PaperBrokerEventRecord:
        """CONTROLLED TEST: confirm a held cancellation (MANUAL mode)."""
        existing = self._by_ref(session, external_ref)
        if existing is not None:
            return existing
        book = self._book(session, client_order_id)
        if book is None or book.status in FINAL_BROKER_STATUSES:
            raise BrokerRefusedError(f"order {client_order_id} is not working at the broker")
        book.status = BrokerOrderStatus.CANCELLED
        return self._event(
            session,
            book,
            BrokerEventType.CANCEL_CONFIRMED,
            at,
            "cancellation confirmed (controlled test event)",
            source=BrokerEventSource.CONTROLLED_TEST,
            external_ref=external_ref,
        )

    def expire_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord | None:
        book = self._book(session, order.id)
        if book is None or book.status in FINAL_BROKER_STATUSES or at < book.expires_at:
            return None
        book.status = BrokerOrderStatus.EXPIRED
        return self._event(
            session,
            book,
            BrokerEventType.EXPIRED,
            at,
            f"expired at {book.expires_at.isoformat()} ({_plain(book.filled_quantity)} of "
            f"{_plain(book.quantity)} filled)",
        )

    def inject_fill(
        self,
        session: Session,
        client_order_id: uuid.UUID,
        *,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        fees: Decimal,
        external_ref: str,
        at: datetime,
    ) -> PaperBrokerEventRecord:
        """CONTROLLED TEST: record a fill of a working order (Phase 7A has no fill model).
        Retrying with the same ``external_ref`` returns the recorded event."""
        existing = self._by_ref(session, external_ref)
        if existing is not None:
            if (
                existing.client_order_id != client_order_id
                or existing.event_type is not BrokerEventType.FILL
                or existing.quantity != quantity
                or existing.price != price
                or existing.commission != commission
                or existing.fees != fees
            ):
                raise BrokerRefusedError(
                    f"controlled test event {external_ref!r} was recorded with other values"
                )
            return existing
        self._require_running(session)
        book = self._book(session, client_order_id)
        if book is None or book.status not in (
            BrokerOrderStatus.WORKING,
            BrokerOrderStatus.PARTIALLY_FILLED,
        ):
            state = "not received" if book is None else book.status.value
            raise BrokerRefusedError(f"order {client_order_id} is {state} at the paper broker")
        left = book.quantity - book.filled_quantity
        if quantity <= ZERO or quantity > left:
            raise BrokerRefusedError(
                f"a fill of {_plain(quantity)} does not fit the {_plain(left)} left"
            )
        book.filled_quantity += quantity
        book.status = (
            BrokerOrderStatus.FILLED if quantity == left else BrokerOrderStatus.PARTIALLY_FILLED
        )
        return self._event(
            session,
            book,
            BrokerEventType.FILL,
            at,
            "controlled test fill (no fill model in Phase 7A)",
            source=BrokerEventSource.CONTROLLED_TEST,
            quantity=quantity,
            price=price,
            commission=commission,
            fees=fees,
            external_ref=external_ref,
        )

    # ---------------------------------------------------------------- simulation (7B)
    def working_book(
        self, session: Session, client_order_id: uuid.UUID
    ) -> PaperBrokerOrderRecord | None:
        """The locked book row of an order the simulator is working (else None)."""
        book = self._book(session, client_order_id)
        if book is None or book.status not in (
            BrokerOrderStatus.WORKING,
            BrokerOrderStatus.PARTIALLY_FILLED,
        ):
            return None
        return book

    def record_trigger(
        self,
        session: Session,
        book: PaperBrokerOrderRecord,
        *,
        level: Decimal,
        at: datetime,
        execution_model_id: int,
        details: dict[str, Any],
    ) -> PaperBrokerEventRecord:
        """SIMULATION: the stop of a working order triggered (recorded once)."""
        self._require_running(session)
        if book.triggered_at is not None:
            raise BrokerRefusedError(f"order {book.client_order_id} has already triggered")
        book.triggered_at = at
        book.trigger_price = level
        return self._event(
            session,
            book,
            BrokerEventType.TRIGGERED,
            at,
            f"stop {_plain(level)} triggered (simulated, {details.get('source', '?')})",
            source=BrokerEventSource.SIMULATION,
            execution_model_id=execution_model_id,
            details=details,
        )

    def record_simulated_fill(
        self,
        session: Session,
        book: PaperBrokerOrderRecord,
        *,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        fees: Decimal,
        at: datetime,
        execution_model_id: int,
        reason: str,
        details: dict[str, Any],
    ) -> PaperBrokerEventRecord:
        """SIMULATION: a fill the fill model derived from stored market data."""
        self._require_running(session)
        if book.status not in (BrokerOrderStatus.WORKING, BrokerOrderStatus.PARTIALLY_FILLED):
            raise BrokerRefusedError(
                f"order {book.client_order_id} is {book.status.value} at the paper broker"
            )
        left = book.quantity - book.filled_quantity
        if quantity <= ZERO or quantity > left:
            raise BrokerRefusedError(
                f"a fill of {_plain(quantity)} does not fit the {_plain(left)} left"
            )
        book.filled_quantity += quantity
        book.commission_charged += commission
        book.status = (
            BrokerOrderStatus.FILLED if quantity == left else BrokerOrderStatus.PARTIALLY_FILLED
        )
        return self._event(
            session,
            book,
            BrokerEventType.FILL,
            at,
            reason,
            source=BrokerEventSource.SIMULATION,
            quantity=quantity,
            price=price,
            commission=commission,
            fees=fees,
            execution_model_id=execution_model_id,
            details=details,
        )

    def _by_ref(self, session: Session, external_ref: str) -> PaperBrokerEventRecord | None:
        return session.scalar(
            select(PaperBrokerEventRecord).where(
                PaperBrokerEventRecord.external_ref == external_ref
            )
        )

    def order_status(self, session: Session, client_order_id: uuid.UUID) -> BrokerOrderView | None:
        book = session.scalar(
            select(PaperBrokerOrderRecord).where(
                PaperBrokerOrderRecord.client_order_id == client_order_id
            )
        )
        if book is None:
            return None
        return BrokerOrderView(
            broker_order_id=book.id,
            client_order_id=book.client_order_id,
            status=book.status,
            quantity=book.quantity,
            filled_quantity=book.filled_quantity,
            last_event_sequence=book.last_event_sequence,
            expires_at=book.expires_at,
        )

    def events_after(
        self, session: Session, client_order_id: uuid.UUID, sequence: int
    ) -> list[PaperBrokerEventRecord]:
        return list(
            session.scalars(
                select(PaperBrokerEventRecord)
                .where(
                    PaperBrokerEventRecord.client_order_id == client_order_id,
                    PaperBrokerEventRecord.sequence > sequence,
                )
                .order_by(PaperBrokerEventRecord.sequence)
            ).all()
        )

    # ---------------------------------------------------------------- lifecycle
    def health(self, session: Session) -> BrokerHealth:
        """Read only (a broker that was never started reports STOPPED)."""
        state = session.get(PaperBrokerStateRecord, self.broker_id)
        working = int(
            session.scalar(
                select(func.count())
                .select_from(PaperBrokerOrderRecord)
                .where(
                    PaperBrokerOrderRecord.broker_id == self.broker_id,
                    PaperBrokerOrderRecord.status.in_(
                        [BrokerOrderStatus.WORKING, BrokerOrderStatus.PARTIALLY_FILLED]
                    ),
                )
            )
            or 0
        )
        if state is None:
            return BrokerHealth(
                broker_id=self.broker_id,
                simulated=True,
                status=BrokerRunState.STOPPED,
                accepting_orders=False,
                fill_model=FILL_MODEL,
                last_heartbeat_at=None,
                last_recovery_at=None,
                working_orders=working,
                details={"never_started": True},
            )
        return BrokerHealth(
            broker_id=state.broker_id,
            simulated=state.simulated,
            status=state.status,
            accepting_orders=state.status is BrokerRunState.RUNNING,
            fill_model=state.fill_model,
            last_heartbeat_at=state.last_heartbeat_at,
            last_recovery_at=state.last_recovery_at,
            working_orders=working,
            details=dict(state.details),
        )

    def _set(self, session: Session, status: BrokerRunState, at: datetime, **fields: Any) -> None:
        state = self._state(session)
        state.status = status
        for name, value in fields.items():
            setattr(state, name, value)
        state.updated_at = func.now()
        session.flush()

    def start(self, session: Session, at: datetime) -> BrokerRecovery:
        self._set(
            session,
            BrokerRunState.RUNNING,
            at,
            fill_model=FILL_MODEL,
            started_at=at,
            last_recovery_at=at,
            last_heartbeat_at=at,
            stopped_at=None,
        )
        working = session.scalars(
            select(PaperBrokerOrderRecord.client_order_id)
            .where(
                PaperBrokerOrderRecord.broker_id == self.broker_id,
                PaperBrokerOrderRecord.status.in_(
                    [BrokerOrderStatus.WORKING, BrokerOrderStatus.PARTIALLY_FILLED]
                ),
            )
            .order_by(PaperBrokerOrderRecord.client_order_id)
        ).all()
        return BrokerRecovery(self.broker_id, list(working))

    def pause(self, session: Session, at: datetime, reason: str) -> None:
        state = self._state(session)
        self._set(
            session, BrokerRunState.PAUSED, at, details={**state.details, "pause_reason": reason}
        )

    def heartbeat(self, session: Session, at: datetime) -> None:
        state = self._state(session)
        state.last_heartbeat_at = at
        state.updated_at = func.now()
        session.flush()

    def shutdown(self, session: Session, at: datetime) -> None:
        self._set(session, BrokerRunState.STOPPED, at, stopped_at=at)


__all__ = ["EVENT_TOPIC", "FILL_MODEL", "ControlledPaperBroker"]

"""The paper execution engine: order management on top of Phase 6C reservations (Phase 7A).

**Paper only.** Orders are simulated and handled by the isolated paper broker; there is
no brokerage connection anywhere in Atlas (spec 15, 24.1).

Every command is one PostgreSQL transaction (READ COMMITTED) that follows the 6C locking
protocol: the ``portfolio_accounts`` row first (the lock every 6A posting, 6B assessment
and 6C reservation command takes), then the order row, then the paper broker's book row,
then the 6C reservation state and reservation rows. All writers of an account serialize
on its row, so concurrent workers cannot interleave inside one account and no lock cycle
exists.

Workflow::

    submit (one transaction)
      lock account; idempotency lookup (execution_requests.idempotency_key)
      authorize + validate (validation.py)   -> REFUSED: request recorded, nothing else
      INSERT orders (PROPOSED), event CREATED
      revalidate (breakers, reconciliation block, reservation still active)
        -> event REJECTED, reservation released NOT_SUBMITTED
        -> 6C attach_order_in, event APPROVED, outbox "order.submit"
    COMMIT  (deferred trigger: never PROPOSED; an approved ENTRY order has its reservation)

    outbox "order.submit"  -> paper broker submit -> ACCEPTED / REJECTED broker event
    outbox "paper_broker.event" -> broker events applied in broker sequence order:
      ACCEPTED -> OPEN;  FILL -> 6A posting + 6C consume_in + order_fills + event FILL
      CANCEL_CONFIRMED -> CANCELLED;  EXPIRED -> EXPIRED;  REJECTED -> REJECTED
      and, when the order is final, the 6C reservation's unconsumed remainder is released
      with the matching resolution (or failed, for FAILED)
    cancel: event CANCEL_REQUESTED (-> CANCEL_PENDING), outbox "order.cancel" -> broker;
      the reservation is released only when the broker's confirmation is applied.

The database enforces the state machine (``atlas_order_event_apply``): legal transitions
only, gapless numbering, broker events applied in order and only once, fills matching
their paper broker event and journal entry. This module never updates an order row.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.accounting.posting import last_sequence
from app.accounting.reconciliation import ReconciliationService
from app.core.clocks import Clock
from app.core.exceptions import AtlasError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models import ActorType, PortfolioAccount
from app.database.models.accounting import ReconciliationRunRecord
from app.database.models.execution import (
    ExecutionRequestRecord,
    OrderEventRecord,
    OrderFillRecord,
    OrderRecord,
    OrderRequestRecord,
    OutboxEventRecord,
    PaperBrokerEventRecord,
)
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.database.models.reservations import RiskReservationRecord, SystemHealthEventRecord
from app.database.repositories.audit_log import AuditLogRepository
from app.database.sessions import session_scope
from app.events.outbox import OutboxDispatcher, OutboxMessage, enqueue
from app.execution import state_machine
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    LIVE_ORDER_STATUSES,
    BrokerEventType,
    ExecutionDecision,
    OrderCommand,
    OrderEventSource,
    OrderEventType,
    OrderPurpose,
    OrderStatus,
    OutboxStatus,
)
from app.execution.models import (
    ControlledFill,
    ExecutionConfig,
    ExpiryReport,
    OrderCommandResult,
    OrderRequest,
    RecoveryReport,
    SubmissionResult,
    plain,
)
from app.execution.portfolio import FillPosting, PositionLedger
from app.execution.positions import PositionBook
from app.execution.validation import (
    authorize_submitter,
    check_fill,
    opposite_position,
    position_quantity,
    revalidate_entry,
    validate_entry,
    validate_exit,
)
from app.paper_broker.controlled import EVENT_TOPIC, ControlledPaperBroker
from app.risk import governance
from app.risk.config import canonical_json
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    ActorKind,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    ExecutionLink,
    ReleaseResolution,
    Severity,
    TradeDirection,
)
from app.risk.reservations.service import ReservationService, lock_reservation_state
from app.risk.state import breaker_statuses, lock_account, record_breaker_event

logger = get_logger(__name__)
T = TypeVar("T")

ENGINE_ACTOR = "system:execution-engine"
RECOVERY_ACTOR = "system:execution-recovery"
EXPIRY_ACTOR = "system:execution-expiry"
SUBMIT_TOPIC = "order.submit"
CANCEL_TOPIC = "order.cancel"
RECONCILE_TOPIC = "accounting.reconcile"
BROKER_TOPIC = EVENT_TOPIC
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:@/-]{8,160}$")
RETRYABLE_SQLSTATES = {"40001", "40P01"}
MAX_ATTEMPTS = 4

_BROKER_TO_ORDER: dict[BrokerEventType, OrderEventType] = {
    BrokerEventType.ACCEPTED: OrderEventType.ACCEPTED,
    BrokerEventType.REJECTED: OrderEventType.REJECTED,
    BrokerEventType.FILL: OrderEventType.FILL,
    BrokerEventType.CANCEL_CONFIRMED: OrderEventType.CANCEL_CONFIRMED,
    BrokerEventType.CANCEL_REJECTED: OrderEventType.CANCEL_REJECTED,
    BrokerEventType.EXPIRED: OrderEventType.EXPIRED,
    BrokerEventType.TRIGGERED: OrderEventType.TRIGGERED,
}
_RESOLUTIONS: dict[OrderStatus, ReleaseResolution] = {
    OrderStatus.FILLED: ReleaseResolution.ORDER_COMPLETED,
    OrderStatus.CANCELLED: ReleaseResolution.ORDER_CANCELLED,
    OrderStatus.EXPIRED: ReleaseResolution.ORDER_EXPIRED,
    OrderStatus.REJECTED: ReleaseResolution.ORDER_REJECTED,
}


class ExecutionError(AtlasError):
    """An execution command was refused. Nothing was changed."""

    code = "execution_refused"
    http_status = 409


class IdempotencyConflictError(ExecutionError):
    code = "idempotency_conflict"


class FillRefusedError(ExecutionError):
    """A controlled test fill violates the order (limit, protection, quantity)."""

    code = "fill_refused"


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _key(value: str) -> str:
    if not KEY_PATTERN.match(value or ""):
        raise InvalidRequestError(
            "an idempotency key is 8 to 160 characters of letters, digits and ._:@/-"
        )
    return value


def _actor(value: str) -> str:
    if not (value or "").strip():
        raise InvalidRequestError("an actor is required")
    return value.strip()


def _why(value: str) -> str:
    if len((value or "").strip()) < 3:
        raise InvalidRequestError("a reason is required")
    return value.strip()


def _source_of(actor: str, default: OrderEventSource) -> OrderEventSource:
    return OrderEventSource.OPERATOR if actor.startswith("operator:") else default


class _OrderEvents:
    """Appends order events after checking the transition with the state machine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        order: OrderRecord,
        event_type: OrderEventType,
        to_status: OrderStatus,
        *,
        at: datetime,
        source: OrderEventSource,
        actor: str,
        reason: str,
        quantity: Decimal = Decimal(0),
        price: Decimal | None = None,
        fill_id: uuid.UUID | None = None,
        broker_event: PaperBrokerEventRecord | None = None,
        details: dict[str, Any] | None = None,
    ) -> OrderEventRecord:
        first = order.last_event_sequence == 0
        current = None if first else order.status
        state_machine.check(current, event_type, to_status)
        event = OrderEventRecord(
            order_id=order.id,
            sequence=order.last_event_sequence + 1,
            event_type=event_type,
            from_status=current,
            to_status=to_status,
            quantity=quantity,
            price=price,
            fill_id=fill_id,
            broker_event_id=None if broker_event is None else broker_event.id,
            broker_sequence=None if broker_event is None else broker_event.sequence,
            source=source,
            actor=actor,
            reason=reason[:2000],
            occurred_at=at,
            details=details or {},
        )
        self.session.add(event)
        self.session.flush()
        self.session.refresh(order)  # the trigger changed the projection
        logger.info(
            "execution.order_event",
            simulated=True,
            order_id=str(order.id),
            event_type=event_type.value,
            status=to_status.value,
        )
        return event


class ExecutionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        accounting: AccountingSettings,
        *,
        broker: ControlledPaperBroker,
        reservations: ReservationService,
        ledger: PositionLedger,
        config: ExecutionConfig | None = None,
        positions: PositionBook | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._accounting = accounting
        self.broker = broker
        self._reservations = reservations
        self._ledger = ledger
        self.config = config or ExecutionConfig()
        self.positions = positions or PositionBook()
        self._books_reconciliation = ReconciliationService(session_factory, clock)

    # ------------------------------------------------------------------ transactions
    def _transaction(self, work: Callable[[Session], T]) -> T:
        """Run ``work`` in its own transaction; retry serialization failures and deadlocks,
        and once a uniqueness race (the retry then finds the committed winner)."""
        unique_retried = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with session_scope(self._factory) as session:
                    return work(session)
            except IntegrityError as exc:
                if unique_retried or getattr(exc.orig, "sqlstate", None) != "23505":
                    raise
                unique_retried = True
                logger.warning("execution.unique_race", attempt=attempt)
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if state not in RETRYABLE_SQLSTATES or attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("execution.retry", sqlstate=state, attempt=attempt)
        raise ExecutionError("the command could not be completed")  # pragma: no cover

    # ------------------------------------------------------------------ submission
    def submit(
        self, request: OrderRequest, *, idempotency_key: str, requested_by: str
    ) -> SubmissionResult:
        return self._transaction(
            lambda s: self.submit_in(
                s, request, idempotency_key=idempotency_key, requested_by=requested_by
            )
        )

    def submit_in(
        self,
        session: Session,
        request: OrderRequest,
        *,
        idempotency_key: str,
        requested_by: str,
    ) -> SubmissionResult:
        key = _key(idempotency_key)
        actor = _actor(requested_by)
        request_sha = _sha({"command": "SUBMIT", "request": request.canonical()})
        try:
            account = lock_account(session, request.account_id)
        except LookupError:
            raise NotFoundError(f"portfolio account {request.account_id} does not exist") from None
        lock_reservation_state(session, account.id)  # 6C protocol step 2 (attach uses it)
        existing = session.scalar(
            select(ExecutionRequestRecord).where(ExecutionRequestRecord.idempotency_key == key)
        )
        if existing is not None:
            return self._replay(session, existing, request_sha)
        at = self._clock.now()
        reasons = authorize_submitter(actor)
        validated = None
        reservation = None
        if not reasons:
            check = validate_entry if request.purpose is OrderPurpose.ENTRY else validate_exit
            reasons, validated, reservation = check(
                session, account, request, actor, at, self.config
            )
        record = ExecutionRequestRecord(
            id=uuid.uuid4(),
            idempotency_key=key,
            request_sha256=request_sha,
            portfolio_account_id=account.id,
            reservation_id=None if reservation is None else reservation.id,
            purpose=request.purpose,
            decision=ExecutionDecision.REFUSED if reasons else ExecutionDecision.ORDER_CREATED,
            reasons=reasons,
            payload=request.canonical(),
            requested_by=actor,
            requested_at=at,
        )
        session.add(record)
        session.flush()
        if reasons or validated is None:
            self._audit(
                session,
                actor,
                "execution.order_refused",
                "execution_request",
                record.id,
                {"reasons": reasons},
            )
            logger.warning(
                "execution.order_refused",
                request_id=str(record.id),
                codes=[r["code"] for r in reasons],
            )
            return SubmissionResult(
                record.id, ExecutionDecision.REFUSED, None, None, reasons, created=True
            )

        r = validated.reservation
        order = OrderRecord(
            id=uuid.uuid4(),
            portfolio_account_id=account.id,
            execution_request_id=record.id,
            idempotency_key=key,
            request_sha256=request_sha,
            asset_id=validated.asset.id,
            symbol=validated.asset.symbol,
            asset_class=validated.asset.asset_class.value,
            quote_currency=validated.asset.quote_currency,
            instrument_spec_id=validated.spec_id,
            quantity_increment=validated.quantity_increment,
            price_increment=validated.price_increment,
            purpose=validated.purpose,
            side=validated.side,
            direction=validated.direction,
            order_type=request.order_type,
            quantity=request.quantity,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            trail_amount=request.trail_amount,
            trail_percent=request.trail_percent,
            protection_price=validated.protection_price,
            status=OrderStatus.PROPOSED,
            filled_quantity=Decimal(0),
            remaining_quantity=request.quantity,
            reservation_id=r.id,
            risk_assessment_id=validated.assessment_id,
            parent_order_id=None if validated.parent is None else validated.parent.id,
            strategy_proposal_id=r.strategy_proposal_id,
            ai_decision_id=r.ai_decision_id,
            requested_by=actor,
            exit_reason=request.exit_reason,
            submitted_at=at,
            expires_at=validated.expires_at,
        )
        session.add(order)
        session.flush()
        events = _OrderEvents(session)
        source = _source_of(actor, OrderEventSource.EXECUTION_ENGINE)
        events.append(
            order,
            OrderEventType.CREATED,
            OrderStatus.PROPOSED,
            at=at,
            source=source,
            actor=actor,
            reason=f"{validated.purpose.value} {order.side.value} {plain(order.quantity)} "
            f"{order.symbol} {order.order_type.value} requested",
            details={"execution_request_id": str(record.id)},
        )
        problems = (
            revalidate_entry(session, account, r, at)
            if validated.purpose is OrderPurpose.ENTRY
            else self._revalidate_exit(account)
        )
        if problems:
            events.append(
                order,
                OrderEventType.REJECTED,
                OrderStatus.REJECTED,
                at=at,
                source=OrderEventSource.EXECUTION_ENGINE,
                actor=ENGINE_ACTOR,
                reason="rejected at pre-execution revalidation: "
                + ", ".join(dict.fromkeys(p["code"] for p in problems)),
                details={"reasons": problems},
            )
            self._settle_reservation(session, order, at)
        else:
            if validated.purpose is OrderPurpose.ENTRY:
                self._reservations.attach_order_in(
                    session,
                    r.id,
                    order_ref=str(order.id),
                    idempotency_key=f"order:{order.id}:attach",
                    actor=ENGINE_ACTOR,
                )
            events.append(
                order,
                OrderEventType.APPROVED,
                OrderStatus.APPROVED,
                at=at,
                source=OrderEventSource.EXECUTION_ENGINE,
                actor=ENGINE_ACTOR,
                reason="authorized and revalidated"
                + ("; reservation attached" if validated.purpose is OrderPurpose.ENTRY else ""),
            )
            enqueue(
                session,
                topic=SUBMIT_TOPIC,
                aggregate_type="order",
                aggregate_id=order.id,
                dedupe_key=f"{SUBMIT_TOPIC}:{order.id}",
                payload={"order_id": str(order.id)},
                at=at,
            )
        self._audit(
            session,
            actor,
            "execution.order_created",
            "order",
            order.id,
            {"status": order.status.value, "reservation_id": str(r.id), "simulated": True},
        )
        return SubmissionResult(
            record.id,
            ExecutionDecision.ORDER_CREATED,
            order.id,
            order.status,
            problems,
            created=True,
        )

    @staticmethod
    def _revalidate_exit(account: PortfolioAccount) -> list[dict[str, Any]]:
        from app.database.models.portfolio_account import PortfolioAccountStatus

        if account.status is not PortfolioAccountStatus.ACTIVE:
            return [{"code": "ACCOUNT_NOT_ACTIVE", "message": f"the account is {account.status}"}]
        return []

    def _replay(
        self, session: Session, existing: ExecutionRequestRecord, request_sha: str
    ) -> SubmissionResult:
        if existing.request_sha256 != request_sha:
            raise IdempotencyConflictError(
                f"idempotency key {existing.idempotency_key!r} was already used for a "
                "different order request"
            )
        order = session.scalar(
            select(OrderRecord)
            .where(OrderRecord.execution_request_id == existing.id)
            .execution_options(populate_existing=True)
        )
        return SubmissionResult(
            existing.id,
            existing.decision,
            None if order is None else order.id,
            None if order is None else order.status,
            existing.reasons,
            created=False,
        )

    # ------------------------------------------------------------------ cancellation
    def request_cancel(
        self, order_id: uuid.UUID, *, idempotency_key: str, actor: str, reason: str
    ) -> OrderCommandResult:
        return self._transaction(
            lambda s: self.request_cancel_in(
                s, order_id, idempotency_key=idempotency_key, actor=actor, reason=reason
            )
        )

    def request_cancel_in(
        self,
        session: Session,
        order_id: uuid.UUID,
        *,
        idempotency_key: str,
        actor: str,
        reason: str,
    ) -> OrderCommandResult:
        """Request cancellation (-> CANCEL_PENDING). The reservation is NOT released here:
        only the paper broker's confirmation, once applied, releases the remainder."""
        key = _key(idempotency_key)
        who = _actor(actor)
        why = _why(reason)
        if authorize_submitter(who):
            raise ExecutionError(f"{who} may not cancel paper orders")
        sha = _sha({"command": "CANCEL", "order_id": str(order_id), "reason": why})
        _account, order = self._lock_order(session, order_id)
        existing = session.scalar(
            select(OrderRequestRecord).where(OrderRequestRecord.idempotency_key == key)
        )
        if existing is not None:
            if existing.request_sha256 != sha or existing.command is not OrderCommand.CANCEL:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} was already used for a different command"
                )
            return OrderCommandResult(order.id, order.status, created=False)
        if order.status not in state_machine.CANCELLABLE:
            raise state_machine.InvalidOrderTransitionError(
                f"order {order.id} is {order.status.value}: it cannot be cancelled"
            )
        at = self._clock.now()
        _OrderEvents(session).append(
            order,
            OrderEventType.CANCEL_REQUESTED,
            OrderStatus.CANCEL_PENDING,
            at=at,
            source=_source_of(who, OrderEventSource.EXECUTION_ENGINE),
            actor=who,
            reason=why,
        )
        session.add(
            OrderRequestRecord(
                idempotency_key=key,
                order_id=order.id,
                command=OrderCommand.CANCEL,
                request_sha256=sha,
                result_status=order.status,
                actor=who,
                reason=why,
            )
        )
        enqueue(
            session,
            topic=CANCEL_TOPIC,
            aggregate_type="order",
            aggregate_id=order.id,
            dedupe_key=f"{CANCEL_TOPIC}:{order.id}",
            payload={"order_id": str(order.id)},
            at=at,
        )
        self._audit(session, who, "execution.cancel_requested", "order", order.id, {"reason": why})
        return OrderCommandResult(order.id, order.status, created=True)

    # ------------------------------------------------------------------ controlled tests
    def record_controlled_fill(
        self, fill: ControlledFill, *, actor: str, apply: bool = True
    ) -> OrderCommandResult:
        """CONTROLLED TEST: record a fill at the paper broker (Phase 7A has no fill model)
        and, with ``apply``, apply it in the same transaction. Only on CONTROLLED_TEST
        accounts; the fill must respect the order (limit, entry protection, quantity)."""
        who = _actor(actor)

        def work(session: Session) -> OrderCommandResult:
            account, order = self._lock_order(session, fill.order_id)
            if account.purpose is not PortfolioAccountPurpose.CONTROLLED_TEST:
                raise ExecutionError(
                    "controlled test fills are recorded only on CONTROLLED_TEST accounts"
                )
            replay = session.scalar(
                select(PaperBrokerEventRecord).where(
                    PaperBrokerEventRecord.external_ref == fill.external_ref
                )
            )
            if replay is None:
                problems = self.revalidate_fill(session, account, order, fill.quantity, fill.price)
                if problems:
                    raise FillRefusedError(
                        "fill refused: " + "; ".join(p["message"] for p in problems)
                    )
            self.broker.inject_fill(
                session,
                order.id,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                fees=fill.fees,
                external_ref=fill.external_ref,
                at=self._clock.now(),
            )
            if apply:
                self._apply_pending(session, order, who)
            return OrderCommandResult(order.id, order.status, created=replay is None)

        return self._transaction(work)

    def revalidate_fill(
        self,
        session: Session,
        account: PortfolioAccount,
        order: OrderRecord,
        quantity: Decimal,
        price: Decimal,
    ) -> list[dict[str, Any]]:
        """The checks a simulated fill must pass *before* the paper broker records it (the
        caller holds the account and order locks): the order is fillable, the quantity fits
        the lot grid and what remains, the price is on the tick grid, within the limit and
        within the entry protection, and no circuit breaker halts new entries. Phase 7A's
        controlled test events go through it; Phase 7B's fill model must too."""
        problems = check_fill(order, quantity, price)
        if order.purpose is OrderPurpose.ENTRY and not problems:
            problems = self._fill_halted(session, account)
            conflict = opposite_position(session, account.id, order.asset_id, order.direction)
            if conflict is not None:
                problems.append(conflict)
        elif order.purpose is OrderPurpose.EXIT and not problems:
            held = self.exit_capacity(session, order)
            if quantity > held:
                problems.append(
                    {
                        "code": "ABOVE_POSITION",
                        "message": f"an exit fill of {plain(quantity)} exceeds the "
                        f"{plain(held)} the position holds",
                    }
                )
        return problems

    def exit_capacity(self, session: Session, order: OrderRecord) -> Decimal:
        """What an EXIT order may still fill: its position's open quantity (7B positions),
        and never more than the 6A ledger holds in the exit's direction."""
        assert order.parent_order_id is not None  # noqa: S101  # database CHECK
        held = position_quantity(session, order.portfolio_account_id, order.asset_id)
        held = held if order.direction is TradeDirection.LONG else -held
        position = self.positions.for_entry(session, order.parent_order_id, lock=False)
        cap = held if position is None else min(held, position.open_quantity)
        return max(min(cap, order.remaining_quantity), Decimal(0))

    @staticmethod
    def _fill_halted(session: Session, account: PortfolioAccount) -> list[dict[str, Any]]:
        tripped = sorted(
            b.value
            for b, s in breaker_statuses(session, account.id).items()
            if s is BreakerStatus.TRIPPED
        )
        if tripped:
            return [
                {
                    "code": "RISK_HALTED",
                    "message": "no entry fills while circuit breaker(s) are tripped: "
                    + ", ".join(tripped),
                }
            ]
        return []

    def confirm_controlled_cancel(
        self, order_id: uuid.UUID, *, external_ref: str, actor: str, apply: bool = True
    ) -> OrderCommandResult:
        """CONTROLLED TEST (MANUAL cancel mode): the paper broker confirms a cancellation."""
        who = _actor(actor)

        def work(session: Session) -> OrderCommandResult:
            account, order = self._lock_order(session, order_id)
            if account.purpose is not PortfolioAccountPurpose.CONTROLLED_TEST:
                raise ExecutionError("controlled test events only on CONTROLLED_TEST accounts")
            self.broker.confirm_cancel(
                session, order.id, self._clock.now(), external_ref=_key(external_ref)
            )
            if apply:
                self._apply_pending(session, order, who)
            return OrderCommandResult(order.id, order.status, created=True)

        return self._transaction(work)

    # ------------------------------------------------------------------ broker events
    def apply_broker_events(self, order_id: uuid.UUID, *, actor: str = ENGINE_ACTOR) -> int:
        def work(session: Session) -> int:
            _account, order = self._lock_order(session, order_id)
            return self._apply_pending(session, order, actor)

        return self._transaction(work)

    def _lock_order(
        self, session: Session, order_id: uuid.UUID
    ) -> tuple[PortfolioAccount, OrderRecord]:
        found = session.get(OrderRecord, order_id)
        if found is None:
            raise NotFoundError(f"order {order_id} does not exist")
        account = lock_account(session, found.portfolio_account_id)
        order = session.scalar(
            select(OrderRecord)
            .where(OrderRecord.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert order is not None  # noqa: S101  # never deleted
        return account, order

    def _apply_pending(self, session: Session, order: OrderRecord, actor: str) -> int:
        """Apply every broker event not yet applied, strictly in broker sequence order (an
        out-of-order delivery applies the earlier ones first; a re-delivery does nothing)."""
        applied = 0
        for event in self.broker.events_after(session, order.id, order.applied_broker_sequence):
            self._apply_one(session, order, event, actor)
            applied += 1
        return applied

    def _apply_one(
        self, session: Session, order: OrderRecord, event: PaperBrokerEventRecord, actor: str
    ) -> None:
        kind = _BROKER_TO_ORDER[event.event_type]
        at = event.occurred_at
        events = _OrderEvents(session)
        if kind is OrderEventType.FILL:
            self._apply_fill(session, order, event, actor)
        else:
            target = state_machine.next_status(
                order.status, kind, quantity=order.quantity, filled=order.filled_quantity
            )
            events.append(
                order,
                kind,
                target,
                at=at,
                source=OrderEventSource.PAPER_BROKER,
                actor=f"system:{self.broker.broker_id}",
                reason=event.reason,
                broker_event=event,
                details={"broker_event_source": event.source.value, "simulated": True},
            )
        if order.status in FINAL_ORDER_STATUSES:
            self._settle_reservation(session, order, at)
            self.positions.on_order_final(session, order, at=at)

    def apply_pending_in(self, session: Session, order: OrderRecord, actor: str) -> int:
        """Apply the order's unapplied paper broker events (the caller holds its locks)."""
        return self._apply_pending(session, order, actor)

    def lock_order(
        self, session: Session, order_id: uuid.UUID
    ) -> tuple[PortfolioAccount, OrderRecord]:
        """Lock the order's account (6C protocol step 1) and then the order."""
        return self._lock_order(session, order_id)

    def transaction(self, work: Callable[[Session], T]) -> T:
        """Run ``work`` in its own transaction with the engine's retry rules."""
        return self._transaction(work)

    def _apply_fill(
        self, session: Session, order: OrderRecord, event: PaperBrokerEventRecord, actor: str
    ) -> None:
        problems = check_fill(order, event.quantity, event.price or Decimal(0))
        if problems:
            # The simulator broke the order's contract: refuse (the delivery is retried and
            # then handled by recovery); nothing is posted.
            raise FillRefusedError(
                f"paper broker event {event.id} cannot be applied: "
                + "; ".join(p["message"] for p in problems)
            )
        assert event.price is not None  # noqa: S101  # check_fill refused a missing price
        target = state_machine.next_status(
            order.status,
            OrderEventType.FILL,
            quantity=order.quantity,
            filled=order.filled_quantity,
            fill=event.quantity,
        )
        posted = self._ledger.post_fill(
            session,
            FillPosting(
                account_id=order.portfolio_account_id,
                order_id=order.id,
                broker_event_id=event.id,
                asset_id=order.asset_id,
                side=order.side,
                quantity=event.quantity,
                price=event.price,
                commission=event.commission,
                fees=event.fees,
                filled_at=event.occurred_at,
                source=event.source,
            ),
            actor=ENGINE_ACTOR,
        )
        reservation_event = None
        if order.purpose is OrderPurpose.ENTRY:
            consumed = self._reservations.consume_in(
                session,
                order.reservation_id,
                quantity=event.quantity,
                journal_entry_id=posted.entry_id,
                idempotency_key=f"fill:{event.id}:consume",
                actor=ENGINE_ACTOR,
                order_ref=str(order.id),
            )
            reservation_event = consumed.event_id
        sequence = (
            session.scalar(
                select(func.count())
                .select_from(OrderFillRecord)
                .where(OrderFillRecord.order_id == order.id)
            )
            or 0
        ) + 1
        fill = OrderFillRecord(
            id=uuid.uuid4(),
            order_id=order.id,
            portfolio_account_id=order.portfolio_account_id,
            asset_id=order.asset_id,
            broker_event_id=event.id,
            journal_entry_id=posted.entry_id,
            reservation_event_id=reservation_event,
            sequence=sequence,
            side=order.side,
            quantity=event.quantity,
            price=event.price,
            commission=event.commission,
            fees=event.fees,
            currency=order.quote_currency,
            source=event.source,
            filled_at=event.occurred_at,
        )
        session.add(fill)
        session.flush()
        # Phase 7B: the position this fill opens, increases or reduces (same transaction).
        self.positions.on_fill(session, order, fill, at=event.occurred_at)
        # 6A reconciliation after every fill (6C/6B: an assessment needs a current one).
        enqueue(
            session,
            topic=RECONCILE_TOPIC,
            aggregate_type="portfolio_account",
            aggregate_id=order.portfolio_account_id,
            dedupe_key=f"{RECONCILE_TOPIC}:{order.portfolio_account_id}:{posted.sequence}",
            payload={
                "account_id": str(order.portfolio_account_id),
                "entry_sequence": posted.sequence,
            },
            at=event.occurred_at,
        )
        _OrderEvents(session).append(
            order,
            OrderEventType.FILL,
            target,
            at=event.occurred_at,
            source=OrderEventSource.PAPER_BROKER,
            actor=f"system:{self.broker.broker_id}",
            reason=f"{plain(event.quantity)} filled at {plain(event.price)} ({event.source.value})",
            quantity=event.quantity,
            price=event.price,
            fill_id=fill.id,
            broker_event=event,
            details={
                "journal_entry_id": str(posted.entry_id),
                "reservation_event_id": reservation_event,
                "applied_by": actor,
                "simulated": True,
            },
        )

    def _settle_reservation(self, session: Session, order: OrderRecord, at: datetime) -> None:
        """Release (or fail) the unconsumed remainder of an ENTRY order's reservation once
        the order is final. Consumed quantity is in the ledger and is never touched."""
        if order.purpose is not OrderPurpose.ENTRY:
            return
        r = session.get(RiskReservationRecord, order.reservation_id, populate_existing=True)
        assert r is not None  # noqa: S101  # foreign key
        if r.status not in ACTIVE_RESERVATION_STATUSES:
            return
        if order.status is OrderStatus.FAILED:
            self._reservations.fail_in(
                session,
                r.id,
                idempotency_key=f"order:{order.id}:fail",
                actor=ENGINE_ACTOR,
                reason=f"order {order.id} FAILED (confirmed): {order.status_reason or ''}",
            )
            return
        resolution = _RESOLUTIONS[order.status]
        if order.status is OrderStatus.REJECTED and r.execution_link is ExecutionLink.NONE:
            resolution = ReleaseResolution.NOT_SUBMITTED
        self._reservations.release_in(
            session,
            r.id,
            resolution=resolution,
            idempotency_key=f"order:{order.id}:release",
            actor=ENGINE_ACTOR,
            reason=f"order {order.id} is {order.status.value}: "
            f"{plain(order.filled_quantity)} filled, the unfilled remainder is released",
        )

    # ------------------------------------------------------------------ outbox handlers
    def handle_submit(self, session: Session, message: OutboxMessage) -> None:
        _account, order = self._lock_order(session, uuid.UUID(message.payload["order_id"]))
        if order.status is not OrderStatus.APPROVED:
            return  # cancelled, expired or already submitted: nothing to do
        self.broker.submit_order(session, order, self._clock.now())
        self._apply_pending(session, order, ENGINE_ACTOR)

    def submit_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        """The submission could not be delivered. The simulator's book says whether the
        order was received: if not, the failure is confirmed (FAILED, reservation failed);
        if it was, its events are applied."""
        _account, order = self._lock_order(session, uuid.UUID(message.payload["order_id"]))
        self._resolve_unsubmitted(session, order, error, OrderEventSource.RECOVERY)

    def _resolve_unsubmitted(
        self, session: Session, order: OrderRecord, error: str, source: OrderEventSource
    ) -> bool:
        if order.status is not OrderStatus.APPROVED:
            return False
        if self.broker.order_status(session, order.id) is not None:
            self._apply_pending(session, order, RECOVERY_ACTOR)
            return False
        at = self._clock.now()
        _OrderEvents(session).append(
            order,
            OrderEventType.FAILED,
            OrderStatus.FAILED,
            at=at,
            source=source,
            actor=RECOVERY_ACTOR,
            reason=f"submission failed and the paper broker never received it: {error}"[:2000],
        )
        self._settle_reservation(session, order, at)
        self._health(
            session,
            order,
            "ORDER_SUBMISSION_FAILED",
            Severity.HIGH,
            f"paper order {order.id} failed before the paper broker received it",
            {"error": error},
        )
        return True

    def handle_cancel(self, session: Session, message: OutboxMessage) -> None:
        _account, order = self._lock_order(session, uuid.UUID(message.payload["order_id"]))
        self._cancel_at_broker(session, order)

    def _cancel_at_broker(self, session: Session, order: OrderRecord) -> None:
        if order.status is not OrderStatus.CANCEL_PENDING:
            self._apply_pending(session, order, ENGINE_ACTOR)
            return
        self.broker.cancel_order(session, order, self._clock.now())
        self._apply_pending(session, order, ENGINE_ACTOR)

    def cancel_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        """The cancellation could not be confirmed: the order stays CANCEL_PENDING, the
        reservation keeps its capacity (flagged uncertain) and EXECUTION_SAFETY trips."""
        account, order = self._lock_order(session, uuid.UUID(message.payload["order_id"]))
        self._uncertain(session, account, order, f"cancellation not confirmed: {error}")

    def handle_broker_event(self, session: Session, message: OutboxMessage) -> None:
        _account, order = self._lock_order(session, uuid.UUID(message.payload["client_order_id"]))
        self._apply_pending(session, order, ENGINE_ACTOR)

    def broker_event_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        account, order = self._lock_order(session, uuid.UUID(message.payload["client_order_id"]))
        self._uncertain(session, account, order, f"paper broker event not applied: {error}")

    def _uncertain(
        self, session: Session, account: PortfolioAccount, order: OrderRecord, why: str
    ) -> None:
        if order.purpose is OrderPurpose.ENTRY:
            r = session.get(RiskReservationRecord, order.reservation_id, populate_existing=True)
            if r is not None and r.status in ACTIVE_RESERVATION_STATUSES:
                self._reservations.flag_uncertain_in(
                    session,
                    r.id,
                    idempotency_key=f"order:{order.id}:uncertain",
                    actor=RECOVERY_ACTOR,
                    reason=why[:500],
                    order_ref=str(order.id),
                )
        self._trip_execution_safety(session, account, order, why)
        self._health(session, order, "EXECUTION_UNCERTAIN", Severity.CRITICAL, why[:500], {})

    def _trip_execution_safety(
        self, session: Session, account: PortfolioAccount, order: OrderRecord, why: str
    ) -> None:
        if breaker_statuses(session, account.id)[BreakerType.EXECUTION_SAFETY] is (
            BreakerStatus.TRIPPED
        ):
            return
        config = governance.active_configuration(session, account.id)
        record_breaker_event(
            session,
            account.id,
            BreakerType.EXECUTION_SAFETY,
            BreakerEvent.TRIPPED,
            at=self._clock.now(),
            actor=RECOVERY_ACTOR,
            actor_kind=ActorKind.SYSTEM,
            reason=f"paper order {order.id}: {why}"[:500],
            measurements={"order_id": str(order.id), "status": order.status.value},
            config_version_id=None if config is None else config.version_id,
        )

    def handle_reconcile(self, session: Session, message: OutboxMessage) -> None:
        """Run the 6A double-entry reconciliation after fills (skipped when a run already
        covers the latest entry). A FAILED run is recorded by 6A like any other."""
        account_id = uuid.UUID(message.payload["account_id"])
        lock_account(session, account_id)
        latest = last_sequence(session, account_id)
        covered = session.scalar(
            select(func.max(ReconciliationRunRecord.entry_sequence)).where(
                ReconciliationRunRecord.portfolio_account_id == account_id
            )
        )
        if covered is not None and covered >= latest:
            return
        self._books_reconciliation.run_in(
            session, account_id, actor=ENGINE_ACTOR, trigger="POST_FILL"
        )

    def reconcile_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        session.add(
            SystemHealthEventRecord(
                component="paper_execution",
                event_type="POST_FILL_RECONCILIATION_FAILED",
                severity=Severity.HIGH,
                portfolio_account_id=uuid.UUID(message.payload["account_id"]),
                subject_kind="portfolio_account",
                subject_id=message.payload["account_id"],
                summary="the reconciliation after a paper fill could not run",
                details={"error": error},
                occurred_at=self._clock.now(),
            )
        )
        session.flush()

    def handlers(self) -> dict[str, Any]:
        return {
            SUBMIT_TOPIC: _Handler(self.handle_submit, self.submit_dead),
            CANCEL_TOPIC: _Handler(self.handle_cancel, self.cancel_dead),
            BROKER_TOPIC: _Handler(self.handle_broker_event, self.broker_event_dead),
            RECONCILE_TOPIC: _Handler(self.handle_reconcile, self.reconcile_dead),
        }

    def dispatcher(self) -> OutboxDispatcher:
        return OutboxDispatcher(
            self._factory,
            self._clock,
            self.handlers(),
            max_attempts=self.config.outbox_max_attempts,
            base_backoff=self.config.outbox_base_backoff,
        )

    # ------------------------------------------------------------------ expiry
    def expire_due(self, *, account_id: uuid.UUID | None = None) -> ExpiryReport:
        """Expire live orders past their expiration. An order the broker holds is expired
        by the broker (its EXPIRED event is applied); an order it never received is expired
        by the engine. Either way the reservation's remainder is released."""
        now = self._clock.now()
        with session_scope(self._factory) as session:
            q = select(OrderRecord.id).where(
                OrderRecord.status.in_(sorted(LIVE_ORDER_STATUSES)),
                OrderRecord.expires_at <= now,
            )
            if account_id is not None:
                q = q.where(OrderRecord.portfolio_account_id == account_id)
            due = list(
                session.scalars(q.order_by(OrderRecord.portfolio_account_id, OrderRecord.id))
            )
        report = ExpiryReport()
        for order_id in due:
            outcome = self._transaction(partial(self._expire_one, order_id=order_id))
            if outcome == "expired":
                report.expired.append(order_id)
            elif outcome == "broker":
                report.sent_to_broker.append(order_id)
        return report

    def _expire_one(self, session: Session, order_id: uuid.UUID) -> str | None:
        _account, order = self._lock_order(session, order_id)
        at = self._clock.now()
        if order.status not in LIVE_ORDER_STATUSES or order.expires_at > at:
            return None
        self._apply_pending(session, order, EXPIRY_ACTOR)
        if order.status in FINAL_ORDER_STATUSES:
            return "expired" if order.status is OrderStatus.EXPIRED else None
        if self.broker.order_status(session, order.id) is None:
            _OrderEvents(session).append(
                order,
                OrderEventType.EXPIRED,
                OrderStatus.EXPIRED,
                at=at,
                source=OrderEventSource.EXPIRY,
                actor=EXPIRY_ACTOR,
                reason=f"expired at {order.expires_at.isoformat()} before the paper broker "
                "received it",
            )
            self._settle_reservation(session, order, at)
            return "expired"
        self.broker.expire_order(session, order, at)
        self._apply_pending(session, order, EXPIRY_ACTOR)
        return "expired" if order.status is OrderStatus.EXPIRED else "broker"

    # ------------------------------------------------------------------ recovery
    def recover(self) -> RecoveryReport:
        """After a restart: start the paper broker, apply every broker event whose delivery
        was lost, resolve submissions and cancellations whose outbox delivery died, and
        expire due orders. Nothing is released without a confirmed resolution."""
        report = RecoveryReport()
        with session_scope(self._factory) as session:
            started = self.broker.start(session, self._clock.now())
            report.broker_working_orders = len(started.working_orders)
            live = list(
                session.scalars(
                    select(OrderRecord.id)
                    .where(OrderRecord.status.in_(sorted(LIVE_ORDER_STATUSES)))
                    .order_by(OrderRecord.portfolio_account_id, OrderRecord.id)
                )
            )
        for order_id in live:
            report.live_orders_checked += 1
            try:
                self._transaction(partial(self._recover_one, order_id=order_id, report=report))
            except AtlasError as exc:
                report.errors.append({"order_id": str(order_id), "error": exc.message})
        report.expired = self.expire_due().expired
        logger.info(
            "execution.recovered",
            live=report.live_orders_checked,
            applied=report.broker_events_applied,
            failed=len(report.failed_unsubmitted),
            errors=len(report.errors),
        )
        return report

    def _recover_one(self, session: Session, order_id: uuid.UUID, report: RecoveryReport) -> None:
        _account, order = self._lock_order(session, order_id)
        report.broker_events_applied += self._apply_pending(session, order, RECOVERY_ACTOR)
        if order.status is OrderStatus.APPROVED:
            submit = self._outbox(session, f"{SUBMIT_TOPIC}:{order.id}")
            lost = submit is None or submit.status is OutboxStatus.DEAD
            if lost and self._resolve_unsubmitted(
                session,
                order,
                "the submission outbox event is missing or dead",
                OrderEventSource.RECOVERY,
            ):
                report.failed_unsubmitted.append(order.id)
        elif order.status is OrderStatus.CANCEL_PENDING:
            cancel = self._outbox(session, f"{CANCEL_TOPIC}:{order.id}")
            if cancel is None or cancel.status is OutboxStatus.DEAD:
                self._cancel_at_broker(session, order)
                report.cancellations_retried.append(order.id)

    @staticmethod
    def _outbox(session: Session, dedupe_key: str) -> OutboxEventRecord | None:
        return session.scalar(
            select(OutboxEventRecord).where(OutboxEventRecord.dedupe_key == dedupe_key)
        )

    # ------------------------------------------------------------------ records
    @staticmethod
    def _audit(
        session: Session,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        details: dict[str, Any],
    ) -> None:
        AuditLogRepository(session).append(
            actor_type=ActorType.USER if actor.startswith("operator:") else ActorType.SUBSYSTEM,
            actor_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=details | {"simulated": True},
        )
        session.flush()

    def _health(
        self,
        session: Session,
        order: OrderRecord,
        event_type: str,
        severity: Severity,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        session.add(
            SystemHealthEventRecord(
                component="paper_execution",
                event_type=event_type,
                severity=severity,
                portfolio_account_id=order.portfolio_account_id,
                subject_kind="order",
                subject_id=str(order.id),
                summary=summary,
                details=details | {"status": order.status.value, "simulated": True},
                occurred_at=self._clock.now(),
            )
        )
        session.flush()


class _Handler:
    def __init__(
        self,
        handle: Callable[[Session, OutboxMessage], None],
        dead: Callable[[Session, OutboxMessage, str], None],
    ) -> None:
        self._handle = handle
        self._dead = dead

    def handle(self, session: Session, message: OutboxMessage) -> None:
        self._handle(session, message)

    def on_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        self._dead(session, message, error)


__all__ = [
    "BROKER_TOPIC",
    "CANCEL_TOPIC",
    "ENGINE_ACTOR",
    "RECONCILE_TOPIC",
    "SUBMIT_TOPIC",
    "ExecutionError",
    "ExecutionService",
    "FillRefusedError",
    "IdempotencyConflictError",
]

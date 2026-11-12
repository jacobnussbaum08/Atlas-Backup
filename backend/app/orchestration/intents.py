"""Trade intents: from a prioritized AI decision to a simulated paper order (Phase 10A).

**Intake (inbox).** New PRIORITIZE_FOR_REVIEW decisions of COMPLETED LIVE coordinator runs
become one ``trade_intents`` row per (account, decision), UNIQUE, written together with an
``orchestration.intent`` outbox event in one transaction. A decision is taken in at most
once per account, however often intake runs or restarts.

**Handling (one transaction, via the outbox).** Under the account lock::

    intent final already                  -> nothing (a re-delivery)
    trading mode not ENABLED              -> NOT_TRADED
    protection gate closed (10B)          -> NOT_TRADED (ENTRY_GATE_CLOSED)
    decision expired                      -> NOT_TRADED
    decision not offerable                -> INVALID
    6C reserve_in (the risk engine decides; key orch:reserve:<intent>)
       REJECTED / INSUFFICIENT_DATA / RISK_HALTED -> RISK_REJECTED (a no-trade decision)
       APPROVED -> 7A submit_in an ENTRY order for the approved quantity
                   (key orch:submit:<intent>; LIMIT at the reserved worst entry price, or
                   MARKET; it may rest until the close of the next regular session)
          refused by execution validation -> release the reservation NOT_SUBMITTED,
                                             EXECUTION_REFUSED
          order created (APPROVED or REJECTED at revalidation) -> ORDER_CREATED or
                                             EXECUTION_REFUSED
    the intent row records the outcome   (all in the same commit as the outbox delivery)

A crash anywhere rolls the whole step back (no reservation, no order, intent still
RECEIVED, event still PENDING); the retry uses the same idempotency keys, so a reservation
or an order can never be created twice. After the last failed attempt the intent is DEAD
(nothing was reserved or ordered: the failed attempts rolled back) and a HIGH
``system_health_events`` row is written.

The orchestrator adds no trading judgement: quantity and levels are the risk engine's
approval; the order type is configuration. Signal strength, scores and rankings are never
read here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clocks import Clock
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.data_engine.calendar import local_date, next_trading_day, trading_day
from app.database.models import AIDecisionRecord
from app.database.models.ai import DecisionRunRecord
from app.database.models.orchestration import TradeIntentRecord
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    SystemHealthEventRecord,
)
from app.events.outbox import OutboxMessage, enqueue
from app.execution.enums import ExecutionDecision, OrderPurpose, OrderStatus, OrderType
from app.execution.models import OrderRequest
from app.execution.service import ExecutionService
from app.orchestration import controls
from app.orchestration.enums import IntentStatus, TradingMode
from app.risk import proposals
from app.risk.enums import ReleaseResolution, ReservationStatus, Severity
from app.risk.reservations.service import ReservationService
from app.risk.state import lock_account

logger = get_logger(__name__)

INTENT_TOPIC = "orchestration.intent"
# Execution authorizes ``system:execution[-...]`` submitters only (7A).
ORCHESTRATOR_ACTOR = "system:execution-orchestrator"
PRIORITIZED = "PRIORITIZE_FOR_REVIEW"
MAX_ORDER_LIFETIME = timedelta(days=6)  # below the execution engine's 7-day maximum


def order_expiry(at: datetime) -> datetime:
    """Close of the regular session in progress at ``at``, else of the next one; an entry
    gets one regular session to fill and never outlives that."""
    today = trading_day(local_date(at))
    if today is not None and at < today.regular_close:
        close = today.regular_close
    else:
        close = next_trading_day(local_date(at)).regular_close
    return min(close, at + MAX_ORDER_LIFETIME)


def pending_decisions(
    session: Session, account_id: uuid.UUID, *, now: datetime, lookback: timedelta, limit: int
) -> list[AIDecisionRecord]:
    """Prioritized decisions of completed LIVE runs, unexpired, recent and not yet taken in
    for this account (oldest first)."""
    taken = select(TradeIntentRecord.id).where(
        TradeIntentRecord.portfolio_account_id == account_id,
        TradeIntentRecord.ai_decision_id == AIDecisionRecord.id,
    )
    return list(
        session.scalars(
            select(AIDecisionRecord)
            .join(DecisionRunRecord, DecisionRunRecord.id == AIDecisionRecord.run_id)
            .where(
                and_(
                    DecisionRunRecord.mode == "LIVE",
                    DecisionRunRecord.status == "COMPLETED",
                    AIDecisionRecord.outcome == PRIORITIZED,
                    AIDecisionRecord.expires_at > now,
                    AIDecisionRecord.decision_at >= now - lookback,
                    AIDecisionRecord.decision_at <= now,
                    ~taken.exists(),
                )
            )
            .order_by(AIDecisionRecord.decision_at, AIDecisionRecord.rank.nulls_last())
            .limit(limit)
        ).all()
    )


def take_in(
    session: Session,
    account_id: uuid.UUID,
    *,
    now: datetime,
    lookback: timedelta,
    limit: int,
) -> list[uuid.UUID]:
    """Record new intents and their outbox events in the caller's transaction."""
    created: list[uuid.UUID] = []
    for d in pending_decisions(session, account_id, now=now, lookback=lookback, limit=limit):
        intent_id = session.execute(
            pg_insert(TradeIntentRecord)
            .values(
                id=uuid.uuid4(),
                portfolio_account_id=account_id,
                ai_decision_id=d.id,
                decision_run_id=d.run_id,
                asset_id=d.asset_id,
                symbol=d.symbol,
                direction=None if d.direction is None else d.direction.value,
                decision_expires_at=d.expires_at,
                status=IntentStatus.RECEIVED,
                received_at=now,
            )
            .on_conflict_do_nothing(index_elements=["portfolio_account_id", "ai_decision_id"])
            .returning(TradeIntentRecord.id)
        ).scalar()
        if intent_id is None:
            continue  # another orchestrator took it in first
        enqueue(
            session,
            topic=INTENT_TOPIC,
            aggregate_type="trade_intent",
            aggregate_id=intent_id,
            dedupe_key=f"{INTENT_TOPIC}:{intent_id}",
            payload={
                "intent_id": str(intent_id),
                "account_id": str(account_id),
                "ai_decision_id": str(d.id),
            },
            at=now,
        )
        created.append(intent_id)
    return created


@dataclass(frozen=True)
class IntentOutcome:
    status: IntentStatus
    reservation_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


class IntentHandler:
    """Outbox handler for ``orchestration.intent`` (see the module docstring)."""

    def __init__(
        self,
        clock: Clock,
        reservations: ReservationService,
        execution: ExecutionService,
        *,
        entry_order_type: OrderType = OrderType.LIMIT,
        gate: Callable[[Session], Any] | None = None,
    ) -> None:
        self._clock = clock
        self._reservations = reservations
        self._execution = execution
        self._order_type = entry_order_type
        # Phase 10B: the protection gate (an object with ``gate_open`` and ``reasons``);
        # closed while the workers that manage positions are not running (D-213).
        self._gate = gate

    # ------------------------------------------------------------------ outbox protocol
    def handle(self, session: Session, message: OutboxMessage) -> None:
        self.process(session, uuid.UUID(message.payload["intent_id"]))

    def on_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        intent = self._lock_intent(session, uuid.UUID(message.payload["intent_id"]))
        if intent is None or intent.status is not IntentStatus.RECEIVED:
            return
        at = self._clock.now()
        self._finish(
            intent, IntentStatus.DEAD, at, reasons=[{"code": "DELIVERY_FAILED"}], error=error
        )
        session.add(
            SystemHealthEventRecord(
                component="orchestration",
                event_type="TRADE_INTENT_DEAD",
                severity=Severity.HIGH,
                portfolio_account_id=intent.portfolio_account_id,
                subject_kind="trade_intent",
                subject_id=str(intent.id),
                summary=f"trade intent for AI decision {intent.ai_decision_id} failed every "
                "attempt; nothing was reserved or ordered",
                details={"error": error[:500]},
                occurred_at=at,
            )
        )
        session.flush()

    # ------------------------------------------------------------------ processing
    @staticmethod
    def _lock_intent(session: Session, intent_id: uuid.UUID) -> TradeIntentRecord | None:
        return session.scalar(
            select(TradeIntentRecord)
            .where(TradeIntentRecord.id == intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _finish(
        intent: TradeIntentRecord,
        status: IntentStatus,
        at: datetime,
        *,
        mode: TradingMode | None = None,
        reasons: list[dict[str, Any]] | None = None,
        reservation_id: uuid.UUID | None = None,
        reservation_decision: str | None = None,
        order_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        error: str | None = None,
    ) -> None:
        intent.status = status
        intent.decided_at = at
        if mode is not None:
            intent.trading_mode = mode
        intent.reasons = reasons or []
        intent.reservation_id = reservation_id
        intent.reservation_decision = reservation_decision
        intent.order_id = order_id
        intent.execution_request_id = request_id
        intent.error = error

    def process(self, session: Session, intent_id: uuid.UUID) -> IntentOutcome:
        # The account lock first (6C/7A protocol step 1): an emergency stop, a reservation
        # and a submission of this account serialize on it.
        head = session.get(TradeIntentRecord, intent_id)
        if head is None:
            raise NotFoundError(f"trade intent {intent_id} does not exist")
        account_id = head.portfolio_account_id
        lock_account(session, account_id)
        intent = self._lock_intent(session, intent_id)
        assert intent is not None  # noqa: S101  # read above
        if intent.status is not IntentStatus.RECEIVED:
            return IntentOutcome(intent.status, intent.reservation_id, intent.order_id)
        at = self._clock.now()
        state = controls.current(session, account_id)
        if not state.entries_allowed:
            self._finish(
                intent,
                IntentStatus.NOT_TRADED,
                at,
                mode=state.mode,
                reasons=[
                    {
                        "code": f"TRADING_{state.mode.value}",
                        "message": f"trading is {state.mode.value}: no new entries",
                    }
                ],
            )
            return IntentOutcome(IntentStatus.NOT_TRADED)
        if self._gate is not None:
            gate = self._gate(session)
            if not gate.gate_open:
                self._finish(
                    intent,
                    IntentStatus.NOT_TRADED,
                    at,
                    mode=state.mode,
                    reasons=[
                        {
                            "code": "ENTRY_GATE_CLOSED",
                            "message": "no new entry while the workers that manage positions "
                            "are not running",
                            "gate": list(gate.reasons),
                        }
                    ],
                )
                return IntentOutcome(IntentStatus.NOT_TRADED)
        if intent.decision_expires_at <= at:
            self._finish(
                intent,
                IntentStatus.NOT_TRADED,
                at,
                mode=state.mode,
                reasons=[
                    {
                        "code": "DECISION_EXPIRED",
                        "message": f"the decision expired at {intent.decision_expires_at}",
                    }
                ],
            )
            return IntentOutcome(IntentStatus.NOT_TRADED)
        try:
            proposal = proposals.from_ai_decision(session, intent.ai_decision_id)
        except (InvalidRequestError, NotFoundError) as exc:
            self._finish(
                intent,
                IntentStatus.INVALID,
                at,
                mode=state.mode,
                reasons=[{"code": "DECISION_NOT_OFFERABLE", "message": str(exc)}],
            )
            return IntentOutcome(IntentStatus.INVALID)

        reserved = self._reservations.reserve_in(
            session,
            account_id,
            proposal,
            idempotency_key=f"orch:reserve:{intent.id}",
            requested_by=ORCHESTRATOR_ACTOR,
        )
        if reserved.decision is not ReservationStatus.APPROVED:
            self._finish(
                intent,
                IntentStatus.RISK_REJECTED,
                at,
                mode=state.mode,
                reasons=reserved.reasons,
                reservation_id=reserved.reservation_id,
                reservation_decision=reserved.decision.value,
            )
            logger.info(
                "orchestration.intent_risk_rejected",
                intent_id=str(intent.id),
                codes=[r.get("code") for r in reserved.reasons][:10],
            )
            return IntentOutcome(IntentStatus.RISK_REJECTED, reserved.reservation_id)

        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == reserved.reservation_id
            )
        )
        assert approval is not None  # noqa: S101  # an APPROVED reservation always has one
        limit = approval.worst_entry_price if self._order_type is OrderType.LIMIT else None
        request = OrderRequest(
            account_id=account_id,
            purpose=OrderPurpose.ENTRY,
            reservation_id=reserved.reservation_id,
            ai_decision_id=intent.ai_decision_id,
            order_type=self._order_type,
            quantity=approval.approved_quantity,
            limit_price=limit,
            expires_at=order_expiry(at),
        )
        submitted = self._execution.submit_in(
            session,
            request,
            idempotency_key=f"orch:submit:{intent.id}",
            requested_by=ORCHESTRATOR_ACTOR,
        )
        if submitted.decision is ExecutionDecision.REFUSED or submitted.order_id is None:
            # Nothing will execute this reservation: give its capacity back now.
            self._reservations.release_in(
                session,
                reserved.reservation_id,
                resolution=ReleaseResolution.NOT_SUBMITTED,
                idempotency_key=f"orch:release:{intent.id}",
                actor=ORCHESTRATOR_ACTOR,
                reason="the execution engine refused the entry order",
            )
            self._finish(
                intent,
                IntentStatus.EXECUTION_REFUSED,
                at,
                mode=state.mode,
                reasons=submitted.reasons,
                reservation_id=reserved.reservation_id,
                reservation_decision=reserved.decision.value,
                request_id=submitted.request_id,
            )
            return IntentOutcome(IntentStatus.EXECUTION_REFUSED, reserved.reservation_id)
        status = (
            IntentStatus.EXECUTION_REFUSED
            if submitted.status is OrderStatus.REJECTED
            else IntentStatus.ORDER_CREATED
        )
        self._finish(
            intent,
            status,
            at,
            mode=state.mode,
            reasons=submitted.reasons,
            reservation_id=reserved.reservation_id,
            reservation_decision=reserved.decision.value,
            order_id=submitted.order_id,
            request_id=submitted.request_id,
        )
        logger.info(
            "orchestration.intent_ordered",
            intent_id=str(intent.id),
            order_id=str(submitted.order_id),
            status=status.value,
            simulated=True,
        )
        return IntentOutcome(status, reserved.reservation_id, submitted.order_id)


__all__ = [
    "INTENT_TOPIC",
    "ORCHESTRATOR_ACTOR",
    "IntentHandler",
    "IntentOutcome",
    "order_expiry",
    "pending_decisions",
    "take_in",
]

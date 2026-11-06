"""Atomic risk reservations (spec 14.3; Phase 6C, decisions D-148 to D-155).

Every command is one PostgreSQL transaction (READ COMMITTED) that follows the same locking
protocol, so concurrent workers serialize on one row per account and cannot deadlock:

1. ``portfolio_accounts`` row ``SELECT ... FOR UPDATE`` (the lock every 6A posting and 6B
   assessment, monitor and breaker action already takes);
2. the account's ``risk_reservation_states`` row ``FOR UPDATE``; its event counter is read
   *before* anything else, and each event must carry the next number (the database refuses
   a stale one with SQLSTATE 40001: an optimistic backstop should any path skip step 1);
3. reservation rows ``FOR UPDATE``, in id order.

Under READ COMMITTED each statement after the lock sees every transaction committed before
the lock was granted, so the state read under the lock is current.

``reserve``: idempotency lookup; expiry of due reservations; identity, expiry and
duplicate checks; the 6B evaluation and the pure assessment recomputed *inside* this
transaction with the active reservations in the state (a worker's earlier assessment is
never trusted); then the reservation, its CREATED and APPROVED/REJECTED events, the approved
order intent, the idempotency record, a risk event and an audit record, all committed
together. The database checks that the approval equals what the in-transaction assessment
permitted.

``attach_order``, ``consume``, ``release``, ``fail``, ``flag_uncertain`` and the expiry
rules are the interface for the execution phase (Phase 7). Here they are exercised only by
controlled test events: nothing in this module submits, fills or executes anything, and no
reservation posts anything to the ledger.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.accounting.money import ZERO, currency
from app.core.clocks import Clock
from app.core.exceptions import AtlasError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models import (
    ActorType,
    AIDecisionRecord,
    Asset,
    PortfolioAccount,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
)
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationEventRecord,
    RiskReservationReconciliationRecord,
    RiskReservationRecord,
    RiskReservationRequestRecord,
    RiskReservationStateRecord,
)
from app.database.models.risk import RiskAssessmentRecord, RiskEventRecord
from app.database.repositories.audit_log import AuditLogRepository
from app.database.sessions import session_scope
from app.risk import governance
from app.risk.assessment import assess
from app.risk.config import canonical_json
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    AssessmentResult,
    CheckOutcome,
    ExecutionLink,
    ProposalSource,
    ReconciliationOutcome,
    ReleaseResolution,
    ReservationCommand,
    ReservationEventType,
    ReservationStatus,
    RiskEventType,
    Severity,
    SnapshotTrigger,
)
from app.risk.models import AssessmentOutcome, Proposal
from app.risk.service import RiskService
from app.risk.sizing import floor_to
from app.risk.state import evaluate, lock_account

logger = get_logger(__name__)
T = TypeVar("T")

APPROVER = "system:risk-reservations"
EXPIRY_ACTOR = "system:reservation-expiry"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:@/-]{8,160}$")
PRIORITIZED = "PRIORITIZE_FOR_REVIEW"
SUPERSEDED = "SUPERSEDED"
RETRYABLE_SQLSTATES = {"40001", "40P01"}  # serialization failure, deadlock detected
MAX_ATTEMPTS = 4


class ReservationError(AtlasError):
    """A reservation command was refused (nothing was changed)."""

    code = "reservation_refused"
    http_status = 409


class IdempotencyConflictError(ReservationError):
    """The idempotency key was already used for a different request."""

    code = "idempotency_conflict"


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _key(value: str) -> str:
    if not KEY_PATTERN.match(value or ""):
        raise InvalidRequestError(
            "an idempotency key is 8 to 160 characters of letters, digits and ._:@/-"
        )
    return value


def _reason(value: str) -> str:
    if len((value or "").strip()) < 3:
        raise InvalidRequestError("a reason is required")
    return value.strip()


def _actor(value: str) -> str:
    if not (value or "").strip():
        raise InvalidRequestError("an actor is required")
    return value.strip()


@dataclass(frozen=True)
class ReservationResult:
    """The outcome of a reservation request (a retry returns the original decision)."""

    reservation_id: uuid.UUID
    decision: ReservationStatus  # APPROVED or REJECTED: what the request decided
    status: ReservationStatus  # the reservation's current status
    proposed_quantity: Decimal | None
    approved_quantity: Decimal
    assessment_id: uuid.UUID | None
    assessment_result: AssessmentResult | None
    expires_at: datetime
    reasons: list[dict[str, Any]]
    created: bool  # False when an earlier request with the same key is returned
    outcome: AssessmentOutcome | None = None


@dataclass(frozen=True)
class CommandResult:
    reservation_id: uuid.UUID
    status: ReservationStatus
    remaining_quantity: Decimal
    consumed_quantity: Decimal
    released_quantity: Decimal
    event_id: int | None
    created: bool


@dataclass
class ExpirySummary:
    expired: list[uuid.UUID] = field(default_factory=list)
    deferred: list[uuid.UUID] = field(default_factory=list)


@dataclass
class _Events:
    """Appends events with the account counter read under the lock (step 2 above)."""

    session: Session
    account_id: uuid.UUID
    sequence: int

    def append(
        self,
        reservation: RiskReservationRecord,
        event_type: ReservationEventType,
        to_status: ReservationStatus,
        *,
        at: datetime,
        actor: str,
        reason: str,
        quantity: Decimal = ZERO,
        amounts: tuple[Decimal, Decimal, Decimal, Decimal] = (ZERO, ZERO, ZERO, ZERO),
        journal_entry_id: uuid.UUID | None = None,
        order_ref: str | None = None,
        resolution: ReleaseResolution | None = None,
        request_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> RiskReservationEventRecord:
        self.sequence += 1
        first = reservation.last_event_sequence == 0
        event = RiskReservationEventRecord(
            reservation_id=reservation.id,
            portfolio_account_id=self.account_id,
            account_sequence=self.sequence,
            reservation_sequence=reservation.last_event_sequence + 1,
            event_type=event_type,
            from_status=None if first else reservation.status,
            to_status=to_status,
            quantity=quantity,
            risk_amount=amounts[0],
            exposure_amount=amounts[1],
            capital_amount=amounts[2],
            margin_amount=amounts[3],
            journal_entry_id=journal_entry_id,
            order_ref=order_ref,
            resolution=resolution,
            request_id=request_id,
            actor=actor,
            reason=reason,
            occurred_at=at,
            details=details or {},
        )
        self.session.add(event)
        self.session.flush()
        # The trigger changed the projection: read it back.
        self.session.refresh(reservation)
        return event


def lock_reservation_state(session: Session, account_id: uuid.UUID) -> RiskReservationStateRecord:
    """Step 2 of the locking protocol (the caller already holds the account row lock)."""
    session.execute(
        pg_insert(RiskReservationStateRecord)
        .values(portfolio_account_id=account_id)
        .on_conflict_do_nothing(index_elements=["portfolio_account_id"])
    )
    state = session.scalar(
        select(RiskReservationStateRecord)
        .where(RiskReservationStateRecord.portfolio_account_id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert state is not None  # noqa: S101  # inserted above
    return state


def approvals_blocked(
    session: Session, account_id: uuid.UUID
) -> RiskReservationReconciliationRecord | None:
    """The latest reservation reconciliation when it FAILED (new approvals are blocked)."""
    latest = session.scalar(
        select(RiskReservationReconciliationRecord)
        .where(RiskReservationReconciliationRecord.portfolio_account_id == account_id)
        .order_by(
            RiskReservationReconciliationRecord.finished_at.desc(),
            RiskReservationReconciliationRecord.created_at.desc(),
        )
        .limit(1)
    )
    if latest is not None and latest.status is ReconciliationOutcome.FAILED:
        return latest
    return None


def _portion(remaining: Decimal, quantity: Decimal, of: Decimal, places: int) -> Decimal:
    """The part of a remaining amount freed by ``quantity`` of ``of`` units, rounded down
    (so the reservation keeps at least its proportion); the whole remainder when all go."""
    if quantity == of:
        return remaining
    return floor_to(remaining * quantity / of, places)


class ReservationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        accounting: AccountingSettings,
        risk: RiskService | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._accounting = accounting
        self._risk = risk or RiskService(session_factory, clock, accounting)

    # ------------------------------------------------------------------ transactions
    def _transaction(self, work: Callable[[Session], T]) -> T:
        """Run ``work`` in its own transaction; retry on a serialization failure or deadlock
        and once on a uniqueness race (the retry then finds the committed winner)."""
        unique_retried = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with session_scope(self._factory) as session:
                    return work(session)
            except IntegrityError as exc:
                if unique_retried or getattr(exc.orig, "sqlstate", None) != "23505":
                    raise
                unique_retried = True
                logger.warning("risk.reservation_unique_race", attempt=attempt)
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if state not in RETRYABLE_SQLSTATES or attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("risk.reservation_retry", sqlstate=state, attempt=attempt)
        raise ReservationError("the reservation could not be completed")  # pragma: no cover

    # ------------------------------------------------------------------ reserve
    def reserve(
        self,
        account_id: uuid.UUID,
        proposal: Proposal,
        *,
        idempotency_key: str,
        requested_by: str,
    ) -> ReservationResult:
        return self._transaction(
            lambda s: self.reserve_in(
                s, account_id, proposal, idempotency_key=idempotency_key, requested_by=requested_by
            )
        )

    def reserve_in(
        self,
        session: Session,
        account_id: uuid.UUID,
        proposal: Proposal,
        *,
        idempotency_key: str,
        requested_by: str,
    ) -> ReservationResult:
        key = _key(idempotency_key)
        actor = _actor(requested_by)
        request_sha = _sha(
            {
                "command": ReservationCommand.RESERVE.value,
                "account_id": str(account_id),
                "proposal": proposal.model_dump(mode="json"),
            }
        )
        # 1-2. the locking protocol; the event counter is read before any capacity input.
        account = lock_account(session, account_id)
        events = _Events(
            session, account_id, lock_reservation_state(session, account_id).last_sequence
        )
        existing = self._request(session, key)
        if existing is not None:
            return self._replay(session, existing, request_sha, ReservationCommand.RESERVE)
        at = self._clock.now()
        self._expire_due(session, events, account_id, at, EXPIRY_ACTOR)
        asset = session.get(Asset, proposal.asset_id)
        if asset is None:
            raise NotFoundError(f"asset {proposal.asset_id} does not exist")

        refusals = self._identity(session, account_id, proposal, at)
        if refusals:
            return self._decide(
                session,
                events,
                account_id,
                asset,
                proposal,
                key,
                request_sha,
                actor,
                at,
                assessment=None,
                outcome=None,
                reasons=refusals,
                expires_at=at,
            )

        # 5-8. state and assessment recomputed inside this transaction, under the lock.
        config = governance.active_configuration(session, account_id)
        evaluated = evaluate(
            session,
            account,
            at,
            config,
            self._accounting,
            persist=True,
            trigger=SnapshotTrigger.ASSESSMENT,
            actor=actor,
        )
        inputs = self._risk._inputs(session, account, asset, proposal, evaluated, config, at)
        outcome = assess(inputs)
        record = self._risk._store(
            session, account_id, asset, inputs, outcome, evaluated, actor, None
        )
        expires_at = record.valid_until
        if proposal.expires_at is not None:
            expires_at = min(expires_at, proposal.expires_at)
        return self._decide(
            session,
            events,
            account_id,
            asset,
            proposal,
            key,
            request_sha,
            actor,
            at,
            assessment=record.id,
            outcome=outcome,
            reasons=_explain(outcome),
            expires_at=expires_at,
        )

    def _decide(
        self,
        session: Session,
        events: _Events,
        account_id: uuid.UUID,
        asset: Asset,
        proposal: Proposal,
        key: str,
        request_sha: str,
        actor: str,
        at: datetime,
        *,
        assessment: uuid.UUID | None,
        outcome: AssessmentOutcome | None,
        reasons: list[dict[str, Any]],
        expires_at: datetime,
    ) -> ReservationResult:
        approve = outcome is not None and outcome.result is AssessmentResult.PASS_FOR_RESERVATION
        if not approve:
            expires_at = at  # a rejection holds nothing, so it is over when it is decided
        reservation = RiskReservationRecord(
            portfolio_account_id=account_id,
            idempotency_key=key,
            request_sha256=request_sha,
            source=proposal.source,
            strategy_proposal_id=proposal.strategy_proposal_id,
            ai_decision_id=proposal.ai_decision_id,
            assessment_id=assessment,
            asset_id=asset.id,
            symbol=asset.symbol,
            asset_class=asset.asset_class.value,
            direction=proposal.direction,
            quote_currency=asset.quote_currency,
            functional_currency=_functional(session, account_id),
            entry=proposal.entry,
            stop=proposal.stop,
            target=proposal.target,
            proposed_quantity=proposal.requested_quantity,
            fx_rate=Decimal(outcome.calculations["fx_rate"])
            if approve and outcome is not None
            else None,
            status=ReservationStatus.PENDING,
            expires_at=expires_at,
            reasons=reasons,
            requested_by=actor,
            requested_at=at,
        )
        session.add(reservation)
        session.flush()
        decision = ReservationStatus.APPROVED if approve else ReservationStatus.REJECTED
        request = self._record_request(
            session,
            key,
            account_id,
            ReservationCommand.RESERVE,
            request_sha,
            reservation.id,
            decision,
            actor,
        )
        events.append(
            reservation,
            ReservationEventType.CREATED,
            ReservationStatus.PENDING,
            at=at,
            actor=actor,
            reason="reservation requested",
            request_id=request.id,
            details={"proposal": proposal.model_dump(mode="json")},
        )
        if approve:
            assert outcome is not None  # noqa: S101  # approve implies an outcome
            reserved = outcome.calculations["reservation"]
            quantity = Decimal(reserved["quantity"])
            amounts = tuple(Decimal(reserved[k]) for k in ("risk", "exposure", "capital", "margin"))
            events.append(
                reservation,
                ReservationEventType.APPROVED,
                ReservationStatus.APPROVED,
                at=at,
                actor=APPROVER,
                reason=_summary(reasons, "approved"),
                quantity=quantity,
                amounts=amounts,  # type: ignore[arg-type]
                request_id=request.id,
                details={"binding_constraint": outcome.binding_constraint, "reasons": reasons},
            )
            self._approval(session, reservation, outcome, at)
        else:
            events.append(
                reservation,
                ReservationEventType.REJECTED,
                ReservationStatus.REJECTED,
                at=at,
                actor=APPROVER,
                reason=_summary(reasons, "rejected"),
                request_id=request.id,
                details={"reasons": reasons},
            )
        self._notify(
            session,
            account_id,
            reservation,
            RiskEventType.RESERVATION_APPROVED if approve else RiskEventType.RESERVATION_REJECTED,
            Severity.INFO,
            at,
            actor,
            f"{reservation.symbol} {reservation.direction.value}: "
            + (
                f"{reservation.approved_quantity.normalize()} reserved"
                + (
                    f" of {proposal.requested_quantity} proposed"
                    if proposal.requested_quantity is not None
                    else ""
                )
                if approve
                else "rejected"
            ),
            {"reasons": reasons, "assessment_id": None if assessment is None else str(assessment)},
        )
        logger.info(
            "risk.reservation_decided",
            account_id=str(account_id),
            reservation_id=str(reservation.id),
            symbol=reservation.symbol,
            decision=decision.value,
            approved_quantity=str(reservation.approved_quantity),
        )
        return ReservationResult(
            reservation_id=reservation.id,
            decision=decision,
            status=reservation.status,
            proposed_quantity=reservation.proposed_quantity,
            approved_quantity=reservation.approved_quantity,
            assessment_id=assessment,
            assessment_result=None if outcome is None else outcome.result,
            expires_at=reservation.expires_at,
            reasons=reasons,
            created=True,
            outcome=outcome,
        )

    def _approval(
        self,
        session: Session,
        reservation: RiskReservationRecord,
        outcome: AssessmentOutcome,
        at: datetime,
    ) -> None:
        reserved = outcome.calculations["reservation"]
        assert reservation.assessment_id is not None  # noqa: S101  # approved => assessed
        content = {
            "reservation_id": str(reservation.id),
            "assessment_id": str(reservation.assessment_id),
            "asset_id": str(reservation.asset_id),
            "direction": reservation.direction.value,
            "approved_quantity": reserved["quantity"],
            "worst_entry_price": reserved["worst_entry_price"],
            "stop": outcome.calculations["stop"],
            "valid_until": reservation.expires_at.isoformat(),
        }
        session.add(
            RiskReservationApprovalRecord(
                reservation_id=reservation.id,
                assessment_id=reservation.assessment_id,
                portfolio_account_id=reservation.portfolio_account_id,
                asset_id=reservation.asset_id,
                direction=reservation.direction,
                proposed_quantity=reservation.proposed_quantity,
                approved_quantity=reservation.approved_quantity,
                entry=reservation.entry,
                worst_entry_price=Decimal(reserved["worst_entry_price"]),
                stop=Decimal(outcome.calculations["stop"]),
                target=reservation.target,
                potential_loss=reservation.approved_risk,
                notional=reservation.approved_exposure,
                approved_at=at,
                valid_until=reservation.expires_at,
                approved_by=APPROVER,
                content_sha256=_sha(content),
            )
        )
        session.flush()

    def _identity(
        self, session: Session, account_id: uuid.UUID, proposal: Proposal, at: datetime
    ) -> list[dict[str, Any]]:
        """Step 2 of the workflow: who proposed it, is it still valid, is it a duplicate,
        may anything be approved at all. Refusals here reserve nothing."""
        out: list[dict[str, Any]] = []

        def refuse(code: str, message: str) -> None:
            out.append({"code": code, "outcome": "FAIL", "check": "identity", "message": message})

        if proposal.expires_at is not None and proposal.expires_at <= at:
            refuse("PROPOSAL_EXPIRED", f"the proposal expired at {proposal.expires_at.isoformat()}")
        if (
            proposal.source is ProposalSource.STRATEGY_PROPOSAL
            and proposal.strategy_proposal_id is None
        ):
            refuse("PROPOSAL_IDENTITY", "a strategy proposal source needs its proposal id")
        if proposal.source is ProposalSource.AI_DECISION and proposal.ai_decision_id is None:
            refuse("PROPOSAL_IDENTITY", "an AI decision source needs its decision id")
        if proposal.strategy_proposal_id is not None:
            row = session.get(StrategyProposalRecord, proposal.strategy_proposal_id)
            if row is None:
                refuse(
                    "PROPOSAL_IDENTITY",
                    f"strategy proposal {proposal.strategy_proposal_id} does not exist",
                )
            else:
                if (
                    row.asset_id != proposal.asset_id
                    or row.direction.value != proposal.direction.value
                    or row.reference_entry != proposal.entry
                    or row.invalidation_level != proposal.stop
                ):
                    refuse(
                        "PROPOSAL_IDENTITY",
                        "the asset, direction, entry or stop differ from the stored strategy "
                        "proposal",
                    )
                if row.expires_at is not None and row.expires_at <= at:
                    refuse(
                        "PROPOSAL_EXPIRED",
                        f"the strategy proposal expired at {row.expires_at.isoformat()}",
                    )
                superseded = session.scalar(
                    select(StrategyProposalEventRecord.id).where(
                        StrategyProposalEventRecord.proposal_id == row.id,
                        # Compared as stored: the risk engine imports no strategy code.
                        StrategyProposalEventRecord.kind == SUPERSEDED,
                        StrategyProposalEventRecord.occurred_at <= at,
                    )
                )
                if superseded is not None:
                    refuse("PROPOSAL_SUPERSEDED", "a newer strategy proposal superseded it")
        if proposal.ai_decision_id is not None:
            decision = session.get(AIDecisionRecord, proposal.ai_decision_id)
            if decision is None:
                refuse("PROPOSAL_IDENTITY", f"AI decision {proposal.ai_decision_id} does not exist")
            elif decision.outcome.value != PRIORITIZED:
                refuse(
                    "PROPOSAL_IDENTITY",
                    f"AI decision outcome {decision.outcome.value} is not {PRIORITIZED}",
                )
            elif decision.expires_at is not None and decision.expires_at <= at:
                refuse(
                    "PROPOSAL_EXPIRED",
                    f"the AI decision expired at {decision.expires_at.isoformat()}",
                )
        for column, value in (
            (RiskReservationRecord.strategy_proposal_id, proposal.strategy_proposal_id),
            (RiskReservationRecord.ai_decision_id, proposal.ai_decision_id),
        ):
            if value is None:
                continue
            active = session.scalar(
                select(RiskReservationRecord.id).where(
                    column == value,
                    RiskReservationRecord.status.in_(ACTIVE_RESERVATION_STATUSES),
                )
            )
            if active is not None:
                refuse(
                    "DUPLICATE_ACTIVE_RESERVATION",
                    f"reservation {active} already holds capacity for this intent",
                )
        blocked = approvals_blocked(session, account_id)
        if blocked is not None:
            refuse(
                "RESERVATION_RECONCILIATION_FAILED",
                f"approvals are blocked: reservation reconciliation {blocked.id} FAILED with "
                f"{blocked.unexplained} unexplained finding(s)",
            )
        return out

    # ------------------------------------------------------------------ execution interface
    def attach_order(
        self, reservation_id: uuid.UUID, *, order_ref: str, idempotency_key: str, actor: str
    ) -> CommandResult:
        """Phase 7 interface: an order now depends on the reservation; from here on it can
        never expire automatically (D-152)."""
        return self._command(
            reservation_id,
            ReservationCommand.ATTACH_ORDER,
            idempotency_key,
            actor,
            {"order_ref": order_ref},
            lambda s, r, ev, req, at: ev.append(
                r,
                ReservationEventType.ORDER_ATTACHED,
                r.status,
                at=at,
                actor=actor,
                reason=f"order {order_ref} depends on this reservation",
                order_ref=_ref(order_ref),
                request_id=req,
            ),
        )

    def consume(
        self,
        reservation_id: uuid.UUID,
        *,
        quantity: Decimal,
        journal_entry_id: uuid.UUID,
        idempotency_key: str,
        actor: str,
        order_ref: str | None = None,
    ) -> CommandResult:
        return self._transaction(
            lambda s: self.consume_in(
                s,
                reservation_id,
                quantity=quantity,
                journal_entry_id=journal_entry_id,
                idempotency_key=idempotency_key,
                actor=actor,
                order_ref=order_ref,
            )
        )

    def consume_in(
        self,
        session: Session,
        reservation_id: uuid.UUID,
        *,
        quantity: Decimal,
        journal_entry_id: uuid.UUID,
        idempotency_key: str,
        actor: str,
        order_ref: str | None = None,
    ) -> CommandResult:
        """Phase 7 interface, normally in the transaction that posts the fill: convert
        ``quantity`` of the reservation into the ledger position of ``journal_entry_id``.
        Only the proportion of the remaining amounts is freed; the remainder stays reserved."""
        if quantity <= ZERO:
            raise InvalidRequestError("a consumed quantity must be positive")

        def apply(
            s: Session, r: RiskReservationRecord, ev: _Events, req: int, at: datetime
        ) -> RiskReservationEventRecord:
            if quantity > r.remaining_quantity:
                raise ReservationError(
                    f"a fill of {quantity} exceeds the {r.remaining_quantity} remaining on "
                    f"reservation {r.id}: an over-fill is an execution error"
                )
            places = currency(r.functional_currency).minor_units
            rem = r.remaining_quantity
            amounts = (
                _portion(r.remaining_risk, quantity, rem, places),
                _portion(r.remaining_exposure, quantity, rem, places),
                _portion(r.remaining_capital, quantity, rem, places),
                _portion(r.remaining_margin, quantity, rem, places),
            )
            final = quantity == rem
            return ev.append(
                r,
                ReservationEventType.CONSUMED,
                ReservationStatus.CONSUMED if final else ReservationStatus.PARTIALLY_CONSUMED,
                at=at,
                actor=actor,
                reason=f"{quantity} filled ({'all' if final else 'part'} of {rem} remaining)",
                quantity=quantity,
                amounts=amounts,
                journal_entry_id=journal_entry_id,
                order_ref=order_ref if order_ref is not None else r.order_ref,
                request_id=req,
            )

        return self._command_in(
            session,
            reservation_id,
            ReservationCommand.CONSUME,
            idempotency_key,
            actor,
            {
                "quantity": str(quantity),
                "journal_entry_id": str(journal_entry_id),
                "order_ref": order_ref,
            },
            apply,
        )

    def release(
        self,
        reservation_id: uuid.UUID,
        *,
        resolution: ReleaseResolution,
        idempotency_key: str,
        actor: str,
        reason: str,
    ) -> CommandResult:
        """Give back the *unconsumed* remainder once the order's fate is confirmed. Filled
        exposure is never reversed by a release: it is in the ledger."""
        why = _reason(reason)
        if resolution is ReleaseResolution.OPERATOR and not actor.startswith("operator:"):
            raise InvalidRequestError("an OPERATOR release needs an operator: actor")

        def apply(
            s: Session, r: RiskReservationRecord, ev: _Events, req: int, at: datetime
        ) -> RiskReservationEventRecord:
            if r.execution_link is ExecutionLink.UNCERTAIN and resolution in (
                ReleaseResolution.NOT_SUBMITTED,
            ):
                raise ReservationError(
                    f"reservation {r.id} is held for review: its execution state is uncertain"
                )
            if (
                r.execution_link is not ExecutionLink.NONE
                and resolution is ReleaseResolution.NOT_SUBMITTED
            ):
                raise ReservationError(
                    f"reservation {r.id} has order {r.order_ref} attached: confirm the order's "
                    "resolution instead of NOT_SUBMITTED"
                )
            return self._give_back(
                ev,
                r,
                ReservationEventType.RELEASED,
                ReservationStatus.RELEASED,
                at,
                actor,
                why,
                req,
                resolution,
            )

        return self._command(
            reservation_id,
            ReservationCommand.RELEASE,
            idempotency_key,
            actor,
            {"resolution": resolution.value, "reason": why},
            apply,
        )

    def fail(
        self, reservation_id: uuid.UUID, *, idempotency_key: str, actor: str, reason: str
    ) -> CommandResult:
        """A *confirmed* downstream failure (nothing is or will be live for it). When the
        execution state is not known, use ``flag_uncertain``: that holds the capacity."""
        why = _reason(reason)

        def apply(
            s: Session, r: RiskReservationRecord, ev: _Events, req: int, at: datetime
        ) -> RiskReservationEventRecord:
            if r.execution_link is ExecutionLink.UNCERTAIN and not actor.startswith("operator:"):
                raise ReservationError(
                    f"reservation {r.id} is held for review: only an operator may fail it"
                )
            return self._give_back(
                ev,
                r,
                ReservationEventType.FAILED,
                ReservationStatus.FAILED,
                at,
                actor,
                why,
                req,
                None,
            )

        return self._command(
            reservation_id, ReservationCommand.FAIL, idempotency_key, actor, {"reason": why}, apply
        )

    def flag_uncertain(
        self,
        reservation_id: uuid.UUID,
        *,
        idempotency_key: str,
        actor: str,
        reason: str,
        order_ref: str | None = None,
    ) -> CommandResult:
        """The execution state is unknown (a worker died mid-submission, a broker timeout):
        the reservation keeps every unit of capacity and can no longer expire on its own."""
        why = _reason(reason)
        return self._command(
            reservation_id,
            ReservationCommand.FLAG_UNCERTAIN,
            idempotency_key,
            actor,
            {"reason": why, "order_ref": order_ref},
            lambda s, r, ev, req, at: ev.append(
                r,
                ReservationEventType.EXECUTION_UNCERTAIN,
                r.status,
                at=at,
                actor=actor,
                reason=why,
                order_ref=None if order_ref is None else _ref(order_ref),
                request_id=req,
            ),
        )

    def _give_back(
        self,
        ev: _Events,
        r: RiskReservationRecord,
        event_type: ReservationEventType,
        status: ReservationStatus,
        at: datetime,
        actor: str,
        reason: str,
        request_id: int | None,
        resolution: ReleaseResolution | None,
    ) -> RiskReservationEventRecord:
        return ev.append(
            r,
            event_type,
            status,
            at=at,
            actor=actor,
            reason=reason,
            quantity=r.remaining_quantity,
            amounts=(
                r.remaining_risk,
                r.remaining_exposure,
                r.remaining_capital,
                r.remaining_margin,
            ),
            resolution=resolution,
            request_id=request_id,
            details={"consumed_quantity_kept": str(r.consumed_quantity)},
        )

    # ------------------------------------------------------------------ command plumbing
    def _command(
        self,
        reservation_id: uuid.UUID,
        command: ReservationCommand,
        idempotency_key: str,
        actor: str,
        payload: dict[str, Any],
        apply: Callable[
            [Session, RiskReservationRecord, _Events, int, datetime], RiskReservationEventRecord
        ],
    ) -> CommandResult:
        return self._transaction(
            lambda s: self._command_in(
                s, reservation_id, command, idempotency_key, actor, payload, apply
            )
        )

    def _command_in(
        self,
        session: Session,
        reservation_id: uuid.UUID,
        command: ReservationCommand,
        idempotency_key: str,
        actor: str,
        payload: dict[str, Any],
        apply: Callable[
            [Session, RiskReservationRecord, _Events, int, datetime], RiskReservationEventRecord
        ],
    ) -> CommandResult:
        key = _key(idempotency_key)
        who = _actor(actor)
        found = session.get(RiskReservationRecord, reservation_id)
        if found is None:
            raise NotFoundError(f"risk reservation {reservation_id} does not exist")
        account_id = found.portfolio_account_id
        request_sha = _sha(
            {"command": command.value, "reservation_id": str(reservation_id), **payload}
        )
        lock_account(session, account_id)
        events = _Events(
            session, account_id, lock_reservation_state(session, account_id).last_sequence
        )
        reservation = session.scalar(
            select(RiskReservationRecord)
            .where(RiskReservationRecord.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert reservation is not None  # noqa: S101  # checked above; never deleted
        existing = self._request(session, key)
        if existing is not None:
            self._check_replay(existing, request_sha, command)
            return CommandResult(
                reservation.id,
                reservation.status,
                reservation.remaining_quantity,
                reservation.consumed_quantity,
                reservation.released_quantity,
                None,
                created=False,
            )
        if reservation.status not in ACTIVE_RESERVATION_STATUSES:
            raise ReservationError(
                f"reservation {reservation.id} is {reservation.status.value}: {command.value} "
                "is not possible"
            )
        at = self._clock.now()
        request = self._record_request(
            session,
            key,
            account_id,
            command,
            request_sha,
            reservation.id,
            _expected_status(command, reservation, payload),
            who,
        )
        event = apply(session, reservation, events, request.id, at)
        if request.result_status is not reservation.status:
            raise ReservationError(  # pragma: no cover - the expected status mirrors apply
                f"{command.value} left {reservation.status.value}, "
                f"expected {request.result_status.value}"
            )
        kind, severity = _EVENT_KINDS[event.event_type]
        self._notify(
            session,
            account_id,
            reservation,
            kind,
            severity,
            at,
            who,
            f"{reservation.symbol}: {event.event_type.value} ({event.reason})",
            {
                "event_id": event.id,
                "quantity": str(event.quantity),
                "remaining_quantity": str(reservation.remaining_quantity),
            },
        )
        return CommandResult(
            reservation.id,
            reservation.status,
            reservation.remaining_quantity,
            reservation.consumed_quantity,
            reservation.released_quantity,
            event.id,
            created=True,
        )

    # ------------------------------------------------------------------ expiry and recovery
    def expire_due(self, account_id: uuid.UUID, *, actor: str = EXPIRY_ACTOR) -> ExpirySummary:
        def work(session: Session) -> ExpirySummary:
            lock_account(session, account_id)
            events = _Events(
                session, account_id, lock_reservation_state(session, account_id).last_sequence
            )
            return self._expire_due(session, events, account_id, self._clock.now(), _actor(actor))

        return self._transaction(work)

    def _expire_due(
        self,
        session: Session,
        events: _Events,
        account_id: uuid.UUID,
        at: datetime,
        actor: str,
    ) -> ExpirySummary:
        """The explicit expiry rule (D-152): a due reservation expires only when nothing was
        attached to it and nothing consumed. Otherwise an order may be live or its state is
        unknown: it is held, marked EXPIRY_DEFERRED once, and waits for the execution state
        to be resolved (consume / release / fail). Nothing is released because a worker
        timed out."""
        summary = ExpirySummary()
        due = session.scalars(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.portfolio_account_id == account_id,
                RiskReservationRecord.status.in_(ACTIVE_RESERVATION_STATUSES),
                RiskReservationRecord.expires_at <= at,
            )
            .order_by(RiskReservationRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for r in due:
            if r.execution_link is ExecutionLink.NONE and r.consumed_quantity == ZERO:
                self._give_back(
                    events,
                    r,
                    ReservationEventType.EXPIRED,
                    ReservationStatus.EXPIRED,
                    at,
                    actor,
                    f"expired at {r.expires_at.isoformat()} with no order attached",
                    None,
                    None,
                )
                summary.expired.append(r.id)
                self._notify(
                    session,
                    account_id,
                    r,
                    RiskEventType.RESERVATION_EXPIRED,
                    Severity.INFO,
                    at,
                    actor,
                    f"{r.symbol}: reservation expired; capacity released",
                    {},
                )
            elif r.expiry_deferred_at is None:
                events.append(
                    r,
                    ReservationEventType.EXPIRY_DEFERRED,
                    r.status,
                    at=at,
                    actor=actor,
                    reason=(
                        f"due at {r.expires_at.isoformat()} but {r.execution_link.value}: "
                        "held until the execution state is resolved"
                    ),
                )
                summary.deferred.append(r.id)
                self._notify(
                    session,
                    account_id,
                    r,
                    RiskEventType.RESERVATION_UNCERTAIN,
                    Severity.WARNING,
                    at,
                    actor,
                    f"{r.symbol}: reservation past expiry held (order {r.order_ref})",
                    {},
                )
        return summary

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _request(session: Session, key: str) -> RiskReservationRequestRecord | None:
        return session.scalar(
            select(RiskReservationRequestRecord).where(
                RiskReservationRequestRecord.idempotency_key == key
            )
        )

    @staticmethod
    def _check_replay(
        existing: RiskReservationRequestRecord, request_sha: str, command: ReservationCommand
    ) -> None:
        if existing.command is not command or existing.request_sha256 != request_sha:
            raise IdempotencyConflictError(
                f"idempotency key {existing.idempotency_key!r} was already used for a "
                f"different {existing.command.value} request"
            )

    def _replay(
        self,
        session: Session,
        existing: RiskReservationRequestRecord,
        request_sha: str,
        command: ReservationCommand,
    ) -> ReservationResult:
        self._check_replay(existing, request_sha, command)
        r = session.get(RiskReservationRecord, existing.reservation_id, populate_existing=True)
        assert r is not None  # noqa: S101  # foreign key
        assessed = (
            None if r.assessment_id is None else session.get(RiskAssessmentRecord, r.assessment_id)
        )
        return ReservationResult(
            reservation_id=r.id,
            decision=existing.result_status,
            status=r.status,
            proposed_quantity=r.proposed_quantity,
            approved_quantity=r.approved_quantity,
            assessment_id=r.assessment_id,
            assessment_result=None if assessed is None else assessed.result,
            expires_at=r.expires_at,
            reasons=r.reasons,
            created=False,
        )

    @staticmethod
    def _record_request(
        session: Session,
        key: str,
        account_id: uuid.UUID,
        command: ReservationCommand,
        request_sha: str,
        reservation_id: uuid.UUID,
        result: ReservationStatus,
        actor: str,
    ) -> RiskReservationRequestRecord:
        request = RiskReservationRequestRecord(
            idempotency_key=key,
            portfolio_account_id=account_id,
            command=command,
            request_sha256=request_sha,
            reservation_id=reservation_id,
            result_status=result,
            actor=actor,
        )
        session.add(request)
        session.flush()
        return request

    @staticmethod
    def _notify(
        session: Session,
        account_id: uuid.UUID,
        reservation: RiskReservationRecord,
        kind: RiskEventType,
        severity: Severity,
        at: datetime,
        actor: str,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        session.add(
            RiskEventRecord(
                portfolio_account_id=account_id,
                event_type=kind,
                severity=severity,
                occurred_at=at,
                actor=actor,
                subject_kind="risk_reservation",
                subject_id=str(reservation.id),
                summary=summary,
                details=details | {"status": reservation.status.value},
            )
        )
        AuditLogRepository(session).append(
            actor_type=ActorType.USER if actor.startswith("operator:") else ActorType.SUBSYSTEM,
            actor_id=actor,
            action=f"risk.{kind.value.lower()}",
            entity_type="risk_reservation",
            entity_id=str(reservation.id),
            new_state={
                "status": reservation.status.value,
                "remaining_quantity": str(reservation.remaining_quantity),
            },
            details=details,
        )
        session.flush()


_EVENT_KINDS: dict[ReservationEventType, tuple[RiskEventType, Severity]] = {
    ReservationEventType.ORDER_ATTACHED: (RiskEventType.RESERVATION_APPROVED, Severity.INFO),
    ReservationEventType.CONSUMED: (RiskEventType.RESERVATION_CONSUMED, Severity.INFO),
    ReservationEventType.RELEASED: (RiskEventType.RESERVATION_RELEASED, Severity.INFO),
    ReservationEventType.FAILED: (RiskEventType.RESERVATION_FAILED, Severity.WARNING),
    ReservationEventType.EXECUTION_UNCERTAIN: (RiskEventType.RESERVATION_UNCERTAIN, Severity.HIGH),
}


def _expected_status(
    command: ReservationCommand, r: RiskReservationRecord, payload: dict[str, Any]
) -> ReservationStatus:
    if command is ReservationCommand.CONSUME:
        q = Decimal(payload["quantity"])
        return (
            ReservationStatus.CONSUMED
            if q == r.remaining_quantity
            else ReservationStatus.PARTIALLY_CONSUMED
        )
    if command is ReservationCommand.RELEASE:
        return ReservationStatus.RELEASED
    if command is ReservationCommand.FAIL:
        return ReservationStatus.FAILED
    return r.status


def _ref(value: str) -> str:
    value = (value or "").strip()
    if not 1 <= len(value) <= 160:
        raise InvalidRequestError("an order reference is 1 to 160 characters")
    return value


def _functional(session: Session, account_id: uuid.UUID) -> str:
    account = session.get(PortfolioAccount, account_id)
    assert account is not None  # noqa: S101  # locked above
    return account.base_currency


def _explain(outcome: AssessmentOutcome) -> list[dict[str, Any]]:
    """Every rejection reason, or every reduction of an approval (D-150)."""
    keep = (
        {CheckOutcome.RESTRICT}
        if outcome.result is AssessmentResult.PASS_FOR_RESERVATION
        else {CheckOutcome.FAIL, CheckOutcome.UNKNOWN, CheckOutcome.HALT}
    )
    out = [
        {
            "code": c.reason_code.value if c.reason_code is not None else c.code.upper(),
            "outcome": c.outcome.value,
            "check": c.code,
            "message": c.message,
            "measured": None if c.measured is None else str(c.measured),
            "limit": None if c.limit is None else str(c.limit),
        }
        for c in outcome.checks
        if c.outcome in keep
    ]
    if outcome.result is not AssessmentResult.PASS_FOR_RESERVATION:
        out.insert(
            0,
            {
                "code": f"ASSESSMENT_{outcome.result.value}",
                "outcome": "FAIL",
                "check": "assessment",
                "message": f"the in-transaction assessment is {outcome.result.value}",
            },
        )
    return out


def _summary(reasons: list[dict[str, Any]], verb: str) -> str:
    if not reasons:
        return f"{verb}: every limit satisfied in full"
    codes = ", ".join(dict.fromkeys(str(r["code"]) for r in reasons))
    return f"{verb}: {codes}"


__all__ = [
    "APPROVER",
    "EXPIRY_ACTOR",
    "CommandResult",
    "ExpirySummary",
    "IdempotencyConflictError",
    "ReservationError",
    "ReservationResult",
    "ReservationService",
    "approvals_blocked",
    "lock_reservation_state",
]

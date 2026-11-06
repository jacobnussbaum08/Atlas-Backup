"""Reservation reconciliation (Phase 6C, decision D-154).

Recomputes every reservation from its append-only events, independently of the database
trigger that maintains the projection, and compares:

* the replayed state with the ``risk_reservations`` projection (status, quantities, every
  approved and remaining amount, execution link, order reference, event count);
* gapless numbering per reservation and per account, and the account counter;
* the account totals in ``risk_reservation_states`` with the sum of active remainders;
* each approval with its approval record and with the assessment recorded in the same
  transaction (passing, same quantity, loss and notional);
* each reservation with its idempotency record;
* each consumption with the ledger: the journal entry exists in the account and moved
  exactly the consumed quantity of the asset in the reservation's direction.

A mismatch is UNEXPLAINED: the run FAILS, a CRITICAL ``system_health_events`` row and a
``RESERVATION_MISMATCH`` risk event are written, and new approvals are refused (by the
service and by the database) until a later run passes. Expected situations (a due
reservation not yet expired, one held past expiry because an order depends on it, one held
for review) are WARNINGS. Nothing is repaired or released automatically.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.money import ZERO
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models import ActorType, AssetQuantityRecord, JournalEntryRecord
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationEventRecord,
    RiskReservationReconciliationRecord,
    RiskReservationRecord,
    RiskReservationRequestRecord,
    RiskReservationStateRecord,
    SystemHealthEventRecord,
)
from app.database.models.risk import RiskAssessmentRecord, RiskEventRecord
from app.database.repositories.audit_log import AuditLogRepository
from app.database.sessions import session_scope
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    AssessmentResult,
    ExecutionLink,
    ReconciliationOutcome,
    ReservationCommand,
    ReservationEventType,
    ReservationStatus,
    RiskEventType,
    Severity,
    TradeDirection,
)
from app.risk.state import lock_account

logger = get_logger(__name__)
COMPONENT = "risk.reservations"
AMOUNTS = ("risk", "exposure", "capital", "margin")
MOVES = (
    ReservationEventType.CONSUMED,
    ReservationEventType.RELEASED,
    ReservationEventType.EXPIRED,
    ReservationEventType.FAILED,
)


@dataclass
class _Replayed:
    status: ReservationStatus | None = None
    approved_quantity: Decimal = ZERO
    consumed_quantity: Decimal = ZERO
    released_quantity: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    execution_link: ExecutionLink = ExecutionLink.NONE
    order_ref: str | None = None
    events: int = 0

    def __post_init__(self) -> None:
        self.approved: dict[str, Decimal] = dict.fromkeys(AMOUNTS, ZERO)
        self.remaining: dict[str, Decimal] = dict.fromkeys(AMOUNTS, ZERO)


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: uuid.UUID
    status: ReconciliationOutcome
    unexplained: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    totals: dict[str, str]


def _replay(events: list[RiskReservationEventRecord]) -> _Replayed:
    """The state machine of ``atlas_risk_reservation_apply``, re-implemented independently."""
    s = _Replayed()
    for e in events:
        s.events += 1
        amounts = {
            "risk": e.risk_amount,
            "exposure": e.exposure_amount,
            "capital": e.capital_amount,
            "margin": e.margin_amount,
        }
        if e.event_type is ReservationEventType.APPROVED:
            s.approved_quantity = s.remaining_quantity = e.quantity
            s.approved = dict(amounts)
            s.remaining = dict(amounts)
        elif e.event_type in MOVES:
            s.remaining_quantity -= e.quantity
            for k in AMOUNTS:
                s.remaining[k] -= amounts[k]
            if e.event_type is ReservationEventType.CONSUMED:
                s.consumed_quantity += e.quantity
            else:
                s.released_quantity += e.quantity
        elif e.event_type is ReservationEventType.ORDER_ATTACHED:
            s.execution_link, s.order_ref = ExecutionLink.ORDER_ATTACHED, e.order_ref
        elif e.event_type is ReservationEventType.EXECUTION_UNCERTAIN:
            s.execution_link = ExecutionLink.UNCERTAIN
            s.order_ref = s.order_ref or e.order_ref
        s.status = e.to_status
    return s


class ReservationReconciler:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    def run(self, account_id: uuid.UUID, *, actor: str) -> ReconciliationResult:
        with session_scope(self._factory) as session:
            return self.run_in(session, account_id, actor=actor)

    def run_in(
        self, session: Session, account_id: uuid.UUID, *, actor: str
    ) -> ReconciliationResult:
        started = self._clock.now()
        # The account lock gives one consistent view: no reservation changes meanwhile.
        lock_account(session, account_id)
        unexplained: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def bad(
            code: str, message: str, reservation: uuid.UUID | None = None, **extra: Any
        ) -> None:
            unexplained.append(
                {"code": code, "message": message, "reservation_id": _s(reservation)}
                | {k: _s(v) for k, v in extra.items()}
            )

        def warn(code: str, message: str, reservation: uuid.UUID) -> None:
            warnings.append({"code": code, "message": message, "reservation_id": str(reservation)})

        reservations = session.scalars(
            select(RiskReservationRecord)
            .where(RiskReservationRecord.portfolio_account_id == account_id)
            .order_by(RiskReservationRecord.requested_at, RiskReservationRecord.id)
            .execution_options(populate_existing=True)
        ).all()
        events = session.scalars(
            select(RiskReservationEventRecord)
            .where(RiskReservationEventRecord.portfolio_account_id == account_id)
            .order_by(RiskReservationEventRecord.account_sequence)
        ).all()
        by_reservation: dict[uuid.UUID, list[RiskReservationEventRecord]] = defaultdict(list)
        for e in events:
            by_reservation[e.reservation_id].append(e)

        # ---- numbering
        sequences = [e.account_sequence for e in events]
        if sequences != list(range(1, len(events) + 1)):
            bad("ACCOUNT_SEQUENCE_GAP", "account event numbers are not 1..n without gaps")
        state = session.scalar(
            select(RiskReservationStateRecord)
            .where(RiskReservationStateRecord.portfolio_account_id == account_id)
            .execution_options(populate_existing=True)
        )
        last = state.last_sequence if state is not None else 0
        if last != len(events):
            bad(
                "ACCOUNT_COUNTER",
                "the account event counter differs from its events",
                expected=len(events),
                found=last,
            )

        totals: dict[str, Decimal] = dict.fromkeys(
            (*AMOUNTS, "long_exposure", "short_exposure"), ZERO
        )
        active = 0
        known = {r.id for r in reservations}
        for orphan in set(by_reservation) - known:
            bad("ORPHAN_EVENTS", "events reference a reservation of another account", orphan)

        for r in reservations:
            evs = by_reservation.get(r.id, [])
            if [e.reservation_sequence for e in evs] != list(range(1, len(evs) + 1)):
                bad("RESERVATION_SEQUENCE_GAP", "reservation events are not 1..k", r.id)
            if not evs or evs[0].event_type is not ReservationEventType.CREATED:
                bad("NOT_CREATED", "the first event is not CREATED", r.id)
            replayed = _replay(evs)
            checks = {
                "status": (replayed.status, r.status),
                "approved_quantity": (replayed.approved_quantity, r.approved_quantity),
                "consumed_quantity": (replayed.consumed_quantity, r.consumed_quantity),
                "released_quantity": (replayed.released_quantity, r.released_quantity),
                "remaining_quantity": (replayed.remaining_quantity, r.remaining_quantity),
                "execution_link": (replayed.execution_link, r.execution_link),
                "order_ref": (replayed.order_ref, r.order_ref),
                "last_event_sequence": (replayed.events, r.last_event_sequence),
            }
            for k in AMOUNTS:
                checks[f"approved_{k}"] = (replayed.approved[k], getattr(r, f"approved_{k}"))
                checks[f"remaining_{k}"] = (replayed.remaining[k], getattr(r, f"remaining_{k}"))
            for name, (expected, found) in checks.items():
                if expected != found:
                    bad(
                        "PROJECTION_MISMATCH",
                        f"{name} differs from the replayed events",
                        r.id,
                        field=name,
                        expected=expected,
                        found=found,
                    )
            if r.status is ReservationStatus.PENDING:
                bad("UNDECIDED", "a reservation was committed PENDING", r.id)
            self._approval(session, r, evs, bad)
            self._request(session, r, bad)
            self._ledger(session, r, evs, bad)
            if r.status in ACTIVE_RESERVATION_STATUSES:
                active += 1
                for k in AMOUNTS:
                    totals[k] += getattr(r, f"remaining_{k}")
                side = "long" if r.direction is TradeDirection.LONG else "short"
                totals[f"{side}_exposure"] += r.remaining_exposure
                if r.expires_at <= started:
                    if r.execution_link is ExecutionLink.NONE:
                        warn("EXPIRY_DUE", "past its expiry and not yet expired (run expire)", r.id)
                    else:
                        warn(
                            "HELD_PAST_EXPIRY",
                            f"past its expiry but {r.execution_link.value}: held",
                            r.id,
                        )
                if r.execution_link is ExecutionLink.UNCERTAIN:
                    warn("EXECUTION_UNCERTAIN", "held for review: execution state unknown", r.id)

        expected_state = {
            "active_count": Decimal(active),
            "reserved_risk": totals["risk"],
            "reserved_exposure": totals["exposure"],
            "reserved_long_exposure": totals["long_exposure"],
            "reserved_short_exposure": totals["short_exposure"],
            "reserved_capital": totals["capital"],
            "reserved_margin": totals["margin"],
        }
        for name, expected in expected_state.items():
            found = Decimal(getattr(state, name)) if state is not None else ZERO
            if found != expected:
                bad(
                    "TOTALS_MISMATCH",
                    f"{name} differs from the active reservations",
                    field=name,
                    expected=expected,
                    found=found,
                )

        status = ReconciliationOutcome.FAILED if unexplained else ReconciliationOutcome.PASSED
        finished = self._clock.now()
        totals_out = {k: str(v) for k, v in totals.items()} | {"active_count": str(active)}
        run = RiskReservationReconciliationRecord(
            portfolio_account_id=account_id,
            status=status,
            started_at=started,
            finished_at=finished,
            account_sequence=last,
            reservations_checked=len(reservations),
            events_checked=len(events),
            unexplained=len(unexplained),
            warnings=len(warnings),
            findings=[f | {"severity": "UNEXPLAINED"} for f in unexplained]
            + [w | {"severity": "WARNING"} for w in warnings],
            totals=totals_out,
            actor=actor,
        )
        session.add(run)
        session.flush()
        failed = status is ReconciliationOutcome.FAILED
        summary = (
            f"reservation reconciliation {status.value}: {len(unexplained)} unexplained, "
            f"{len(warnings)} warning(s), {len(reservations)} reservation(s), "
            f"{len(events)} event(s)"
        )
        session.add(
            RiskEventRecord(
                portfolio_account_id=account_id,
                event_type=RiskEventType.RESERVATION_MISMATCH
                if failed
                else RiskEventType.RESERVATION_RECONCILED,
                severity=Severity.CRITICAL if failed else Severity.INFO,
                occurred_at=finished,
                actor=actor,
                subject_kind="risk_reservation_reconciliation",
                subject_id=str(run.id),
                summary=summary,
                details={"unexplained": unexplained[:20], "warnings": warnings[:20]},
            )
        )
        if failed:
            session.add(
                SystemHealthEventRecord(
                    component=COMPONENT,
                    event_type="RESERVATION_RECONCILIATION_FAILED",
                    severity=Severity.CRITICAL,
                    portfolio_account_id=account_id,
                    subject_kind="risk_reservation_reconciliation",
                    subject_id=str(run.id),
                    summary=summary + ": new reservation approvals are blocked",
                    details={"unexplained": unexplained[:50]},
                    occurred_at=finished,
                )
            )
        AuditLogRepository(session).append(
            actor_type=ActorType.USER if actor.startswith("operator:") else ActorType.SUBSYSTEM,
            actor_id=actor,
            action="risk.reservation_reconciliation",
            entity_type="risk_reservation_reconciliation",
            entity_id=str(run.id),
            new_state={"status": status.value},
            details={"unexplained": len(unexplained), "warnings": len(warnings)},
        )
        session.flush()
        (logger.error if failed else logger.info)(
            "risk.reservation_reconciliation",
            account_id=str(account_id),
            status=status.value,
            unexplained=len(unexplained),
            warnings=len(warnings),
        )
        return ReconciliationResult(run.id, status, unexplained, warnings, totals_out)

    # ------------------------------------------------------------------ cross-checks
    @staticmethod
    def _approval(
        session: Session,
        r: RiskReservationRecord,
        evs: list[RiskReservationEventRecord],
        bad: Any,
    ) -> None:
        approved = [e for e in evs if e.event_type is ReservationEventType.APPROVED]
        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == r.id
            )
        )
        if not approved:
            if approval is not None:
                bad(
                    "APPROVAL_WITHOUT_EVENT",
                    "an approval record exists for an unapproved one",
                    r.id,
                )
            return
        if approval is None or approval.approved_quantity != r.approved_quantity:
            bad("APPROVAL_RECORD", "no approval record matches the approved quantity", r.id)
        a = None if r.assessment_id is None else session.get(RiskAssessmentRecord, r.assessment_id)
        if (
            a is None
            or a.result is not AssessmentResult.PASS_FOR_RESERVATION
            or a.permitted_quantity != r.approved_quantity
            or a.potential_loss != r.approved_risk
            or a.notional != r.approved_exposure
            or a.created_txid != r.created_txid
        ):
            bad(
                "ASSESSMENT_MISMATCH",
                "the approval is not what a passing assessment of the same transaction permitted",
                r.id,
            )

    @staticmethod
    def _request(session: Session, r: RiskReservationRecord, bad: Any) -> None:
        req = session.scalar(
            select(RiskReservationRequestRecord).where(
                RiskReservationRequestRecord.reservation_id == r.id,
                RiskReservationRequestRecord.command == ReservationCommand.RESERVE,
            )
        )
        if (
            req is None
            or req.idempotency_key != r.idempotency_key
            or (req.request_sha256 != r.request_sha256)
        ):
            bad("IDEMPOTENCY_RECORD", "no idempotency record matches the reservation", r.id)

    @staticmethod
    def _ledger(
        session: Session,
        r: RiskReservationRecord,
        evs: list[RiskReservationEventRecord],
        bad: Any,
    ) -> None:
        for e in evs:
            if e.event_type is not ReservationEventType.CONSUMED:
                continue
            entry = (
                None
                if e.journal_entry_id is None
                else session.get(JournalEntryRecord, e.journal_entry_id)
            )
            if entry is None or entry.portfolio_account_id != r.portfolio_account_id:
                bad(
                    "LEDGER_FILL_MISSING",
                    "a consumption has no ledger fill in the account",
                    r.id,
                    event_id=e.id,
                )
                continue
            moved = session.scalar(
                select(AssetQuantityRecord.quantity_delta).where(
                    AssetQuantityRecord.entry_id == entry.id,
                    AssetQuantityRecord.asset_id == r.asset_id,
                )
            )
            expected = e.quantity if r.direction is TradeDirection.LONG else -e.quantity
            if moved != expected:
                bad(
                    "LEDGER_FILL_MISMATCH",
                    "the ledger fill did not move the consumed quantity in this direction",
                    r.id,
                    event_id=e.id,
                    expected=expected,
                    found=moved,
                )


def latest(session: Session, account_id: uuid.UUID) -> RiskReservationReconciliationRecord | None:
    return session.scalar(
        select(RiskReservationReconciliationRecord)
        .where(RiskReservationReconciliationRecord.portfolio_account_id == account_id)
        .order_by(
            RiskReservationReconciliationRecord.finished_at.desc(),
            RiskReservationReconciliationRecord.created_at.desc(),
        )
        .limit(1)
    )


def _s(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["ReconciliationResult", "ReservationReconciler", "latest"]

"""Atomic risk reservations (spec 13, 14.3, 21; Phase 6C, migration 0015).

Reservations hold *capacity* (risk budget, exposure, capital, margin) for an approved trade
intent until execution consumes or releases it. They are never ledger transactions: no
journal entry is posted for a reservation (decision D-149).

* ``risk_reservations``: the current-state projection, one row per reservation. Inserted
  PENDING; afterwards only ``atlas_risk_reservation_apply`` (the event trigger) changes it,
  and the deferred ``atlas_risk_reservation_decided`` refuses to commit a PENDING one.
* ``risk_reservation_events``: append-only history. Every event is numbered per account and
  per reservation without gaps; the trigger checks the transition, the account sequence (an
  optimistic-concurrency backstop to the account row lock) and the arithmetic, and applies
  it to the projection and to ``risk_reservation_states``.
* ``risk_reservation_states``: one row per account: the event counter and the totals of
  active reservations (changed only by the event trigger).
* ``risk_reservation_approvals``: the approved order intent of an APPROVED reservation,
  written in the transaction that approved it, immutable.
* ``risk_reservation_requests``: idempotency records (unique key, request hash, result).
* ``risk_reservation_reconciliations``: append-only reconciliation runs; the latest FAILED
  run blocks new approvals (``atlas_risk_reservation_apply``).
* ``system_health_events``: high-priority operational events (append-only).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.risk.enums import (
    ExecutionLink,
    ProposalSource,
    ReconciliationOutcome,
    ReleaseResolution,
    ReservationCommand,
    ReservationEventType,
    ReservationStatus,
    Severity,
    TradeDirection,
)

AMOUNT = Numeric(28, 8)
QUANTITY = Numeric(38, 18)
PRICE = Numeric(30, 10)
RATE = Numeric(30, 12)
ACTIVE = "status IN ('APPROVED', 'PARTIALLY_CONSUMED')"
FINAL = "status IN ('CONSUMED', 'RELEASED', 'EXPIRED', 'REJECTED', 'FAILED')"
AMOUNTS = ("risk", "exposure", "capital", "margin")


class RiskReservationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_reservations"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("assessment_id"),
        CheckConstraint("length(btrim(idempotency_key)) >= 8", name="key_present"),
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="request_sha256_format"),
        CheckConstraint(
            "approved_quantity >= 0 AND consumed_quantity >= 0 AND released_quantity >= 0 "
            "AND remaining_quantity >= 0",
            name="quantities_non_negative",
        ),
        CheckConstraint(
            "consumed_quantity + released_quantity + remaining_quantity = approved_quantity",
            name="quantity_conservation",
        ),
        CheckConstraint(
            " AND ".join(
                f"approved_{a} >= 0 AND remaining_{a} >= 0 AND remaining_{a} <= approved_{a}"
                for a in AMOUNTS
            ),
            name="amounts_within_approval",
        ),
        CheckConstraint(
            f"NOT ({FINAL}) OR (remaining_quantity = 0 AND "
            + " AND ".join(f"remaining_{a} = 0" for a in AMOUNTS)
            + ")",
            name="final_holds_nothing",
        ),
        CheckConstraint(f"NOT ({ACTIVE}) OR remaining_quantity > 0", name="active_holds_quantity"),
        CheckConstraint(
            "status NOT IN ('PENDING', 'REJECTED') OR (approved_quantity = 0 AND "
            + " AND ".join(f"approved_{a} = 0" for a in AMOUNTS)
            + ")",
            name="unapproved_reserves_nothing",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'REJECTED') OR (approved_quantity > 0 AND "
            "assessment_id IS NOT NULL AND approved_risk > 0 AND approved_exposure > 0 "
            "AND fx_rate IS NOT NULL AND fx_rate > 0)",
            name="approval_is_sized",
        ),
        CheckConstraint(
            "status NOT IN ('PARTIALLY_CONSUMED', 'CONSUMED') OR consumed_quantity > 0",
            name="consumed_has_fills",
        ),
        CheckConstraint(
            "proposed_quantity IS NULL OR approved_quantity <= proposed_quantity",
            name="never_above_proposal",
        ),
        CheckConstraint(
            "(execution_link <> 'NONE' OR order_ref IS NULL) AND "
            "(execution_link <> 'ORDER_ATTACHED' OR order_ref IS NOT NULL)",
            name="order_ref_matches_link",
        ),
        CheckConstraint("last_event_sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("expires_at >= requested_at", name="expires_not_before_request"),
        CheckConstraint("entry > 0", name="entry_positive"),
        # One live reservation per strategy proposal or AI decision (a retry under another
        # key cannot reserve the same intent twice).
        Index(
            "uq_risk_reservations_active_strategy_proposal",
            "strategy_proposal_id",
            unique=True,
            postgresql_where=text(f"strategy_proposal_id IS NOT NULL AND {ACTIVE}"),
        ),
        Index(
            "uq_risk_reservations_active_ai_decision",
            "ai_decision_id",
            unique=True,
            postgresql_where=text(f"ai_decision_id IS NOT NULL AND {ACTIVE}"),
        ),
        Index(None, "portfolio_account_id", "status"),
        Index(None, "portfolio_account_id", "requested_at"),
        Index(None, "asset_id", "status"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[ProposalSource] = mapped_column(
        str_enum(ProposalSource, "risk_reservation_source")
    )
    strategy_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="RESTRICT")
    )
    # The assessment recomputed under the reservation transaction (NULL when the request
    # was refused before any assessment: identity, expiry, duplicate, blocked approvals).
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    direction: Mapped[TradeDirection] = mapped_column(
        str_enum(TradeDirection, "risk_reservation_direction")
    )
    quote_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    functional_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    entry: Mapped[Decimal] = mapped_column(PRICE)
    stop: Mapped[Decimal | None] = mapped_column(PRICE)
    target: Mapped[Decimal | None] = mapped_column(PRICE)
    proposed_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    approved_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    # Functional units per quote unit at approval (amounts below are functional).
    fx_rate: Mapped[Decimal | None] = mapped_column(RATE)
    approved_risk: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    approved_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    approved_capital: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    approved_margin: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    status: Mapped[ReservationStatus] = mapped_column(
        str_enum(ReservationStatus, "risk_reservation_status")
    )
    consumed_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    released_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    remaining_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    remaining_risk: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    remaining_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    remaining_capital: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    remaining_margin: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    execution_link: Mapped[ExecutionLink] = mapped_column(
        str_enum(ExecutionLink, "risk_reservation_execution_link"), server_default="NONE"
    )
    order_ref: Mapped[str | None] = mapped_column(String(160))
    expires_at: Mapped[datetime]
    expiry_deferred_at: Mapped[datetime | None]
    # Every reduction or rejection, explained (reason codes with messages).
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    requested_by: Mapped[str] = mapped_column(String(128))
    last_event_sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    # When it was requested (service clock; ``created_at`` is the database wall time).
    requested_at: Mapped[datetime]
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskReservationRequestRecord(Base):
    """Idempotency records: one per command key, with the request hash and its result."""

    __tablename__ = "risk_reservation_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("length(btrim(idempotency_key)) >= 8", name="key_present"),
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="request_sha256_format"),
        Index(None, "reservation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    command: Mapped[ReservationCommand] = mapped_column(
        str_enum(ReservationCommand, "risk_reservation_command")
    )
    request_sha256: Mapped[str] = mapped_column(String(64))
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    # The status the command left the reservation in (the original result of a retry).
    result_status: Mapped[ReservationStatus] = mapped_column(
        str_enum(ReservationStatus, "risk_reservation_request_result")
    )
    actor: Mapped[str] = mapped_column(String(128))
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskReservationEventRecord(Base):
    __tablename__ = "risk_reservation_events"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "account_sequence"),
        UniqueConstraint("reservation_id", "reservation_sequence"),
        UniqueConstraint("journal_entry_id"),
        CheckConstraint("account_sequence >= 1", name="account_sequence_positive"),
        CheckConstraint("reservation_sequence >= 1", name="reservation_sequence_positive"),
        CheckConstraint(
            "quantity >= 0 AND " + " AND ".join(f"{a}_amount >= 0" for a in AMOUNTS),
            name="magnitudes_non_negative",
        ),
        CheckConstraint(
            "(event_type = 'CONSUMED') = (journal_entry_id IS NOT NULL)",
            name="consumption_references_ledger",
        ),
        CheckConstraint(
            "(event_type = 'RELEASED') = (resolution IS NOT NULL)", name="release_has_resolution"
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        CheckConstraint(
            "resolution IS DISTINCT FROM 'OPERATOR' OR actor ~ '^operator:[A-Za-z0-9._@-]+$'",
            name="operator_release_by_operator",
        ),
        Index(None, "portfolio_account_id", "occurred_at"),
        Index(None, "event_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    account_sequence: Mapped[int] = mapped_column(BigInteger)
    reservation_sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[ReservationEventType] = mapped_column(
        str_enum(ReservationEventType, "risk_reservation_event_type")
    )
    from_status: Mapped[ReservationStatus | None] = mapped_column(
        str_enum(ReservationStatus, "risk_reservation_from_status")
    )
    to_status: Mapped[ReservationStatus] = mapped_column(
        str_enum(ReservationStatus, "risk_reservation_to_status")
    )
    # Magnitudes moved by the event: reserved by APPROVED, taken out of the reservation by
    # CONSUMED / RELEASED / EXPIRED / FAILED, zero otherwise. Functional currency.
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    risk_amount: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    exposure_amount: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    capital_amount: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    margin_amount: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    # The ledger fill a consumption converted into (asset quantity subledger cross-check).
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    order_ref: Mapped[str | None] = mapped_column(String(160))
    resolution: Mapped[ReleaseResolution | None] = mapped_column(
        str_enum(ReleaseResolution, "risk_reservation_resolution")
    )
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("risk_reservation_requests.id", ondelete="RESTRICT")
    )
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskReservationStateRecord(Base):
    """Per account: the reservation event counter and the active totals (trigger-maintained)."""

    __tablename__ = "risk_reservation_states"
    __table_args__ = (
        CheckConstraint("last_sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("active_count >= 0", name="count_non_negative"),
        CheckConstraint(
            " AND ".join(f"reserved_{a} >= 0" for a in AMOUNTS)
            + " AND reserved_long_exposure >= 0 AND reserved_short_exposure >= 0",
            name="totals_non_negative",
        ),
        CheckConstraint(
            "reserved_exposure = reserved_long_exposure + reserved_short_exposure",
            name="gross_is_long_plus_short",
        ),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    active_count: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved_risk: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    reserved_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    reserved_long_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    reserved_short_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    reserved_capital: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    reserved_margin: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskReservationApprovalRecord(UUIDPrimaryKeyMixin, Base):
    """The approved order intent of a reservation (what Phase 7 may submit, at most)."""

    __tablename__ = "risk_reservation_approvals"
    __table_args__ = (
        UniqueConstraint("reservation_id"),
        CheckConstraint("approved_quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "proposed_quantity IS NULL OR approved_quantity <= proposed_quantity",
            name="never_above_proposal",
        ),
        CheckConstraint("worst_entry_price > 0", name="worst_entry_positive"),
        CheckConstraint("valid_until > approved_at", name="valid_after_approval"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"),
    )

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT")
    )
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    direction: Mapped[TradeDirection] = mapped_column(
        str_enum(TradeDirection, "risk_approval_direction")
    )
    proposed_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    approved_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    entry: Mapped[Decimal] = mapped_column(PRICE)
    # The least favourable entry price the sizing allowed for (tick-adjusted adverse entry);
    # an execution worse than this would carry more risk than was reserved.
    worst_entry_price: Mapped[Decimal] = mapped_column(PRICE)
    stop: Mapped[Decimal] = mapped_column(PRICE)
    target: Mapped[Decimal | None] = mapped_column(PRICE)
    potential_loss: Mapped[Decimal] = mapped_column(AMOUNT)
    notional: Mapped[Decimal] = mapped_column(AMOUNT)
    approved_at: Mapped[datetime]
    valid_until: Mapped[datetime]
    approved_by: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskReservationReconciliationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_reservation_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "(status = 'PASSED') = (unexplained = 0)", name="passed_iff_nothing_unexplained"
        ),
        CheckConstraint("finished_at >= started_at", name="finished_after_start"),
        Index(None, "portfolio_account_id", "finished_at"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    status: Mapped[ReconciliationOutcome] = mapped_column(
        str_enum(ReconciliationOutcome, "risk_reservation_reconciliation_status")
    )
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]
    account_sequence: Mapped[int] = mapped_column(BigInteger)
    reservations_checked: Mapped[int] = mapped_column(Integer)
    events_checked: Mapped[int] = mapped_column(Integer)
    unexplained: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[int] = mapped_column(Integer)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    totals: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SystemHealthEventRecord(Base):
    """High-priority operational events (spec 21 ``system_health_events``). Append-only."""

    __tablename__ = "system_health_events"
    __table_args__ = (
        CheckConstraint("length(btrim(summary)) >= 3", name="summary_given"),
        Index(None, "component", "occurred_at"),
        Index(None, "severity", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    component: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "system_health_severity"))
    portfolio_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    subject_kind: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    occurred_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "RiskReservationApprovalRecord",
    "RiskReservationEventRecord",
    "RiskReservationReconciliationRecord",
    "RiskReservationRecord",
    "RiskReservationRequestRecord",
    "RiskReservationStateRecord",
    "SystemHealthEventRecord",
]

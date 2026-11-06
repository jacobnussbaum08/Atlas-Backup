"""Risk engine tables (spec 14, 21; Phase 6B, migration 0014). Append-only.

* ``risk_configurations`` and ``risk_configuration_versions``: one configuration per
  portfolio account; immutable, gapless versions written only by an ``operator:`` actor with
  a reason (``atlas_risk_config_version_guard``).
* ``risk_asset_classifications``: operator sector / correlation-cluster tags, effective-dated.
* ``risk_exposure_snapshots``: the marked portfolio, exposure and loss state at a time.
* ``risk_period_baselines``: start-of-period equity, fixed once per daily/weekly period.
* ``risk_assessments`` and ``risk_assessment_reasons``: every decision and every check.
  ``atlas_risk_assessment_guard`` refuses a PASS_FOR_RESERVATION row while a circuit
  breaker of the account is tripped, against a configuration that is not the latest, or
  with a budget above the configured risk per trade; ``atlas_risk_assessment_passed``
  (deferred to COMMIT) refuses a pass with a failing, unknown or halting check.
* ``risk_circuit_breaker_events``: every trip and reset, numbered per breaker without gaps;
  ``atlas_risk_breaker_guard`` checks each transition. The current state of every breaker is
  the view ``risk_circuit_breaker_states`` (latest event), not a mutable row.
* ``risk_events``: the risk engine's event stream.
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
    ActorKind,
    AssessmentResult,
    BaselineBasis,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    CheckOutcome,
    ClassificationSource,
    ConfigChangeKind,
    MarkStatus,
    PeriodKind,
    ProposalSource,
    RiskEventType,
    Severity,
    SnapshotTrigger,
    TradeDirection,
)

AMOUNT = Numeric(28, 8)
QUANTITY = Numeric(38, 18)
PRICE = Numeric(30, 10)
RATE = Numeric(30, 12)
OPERATOR_ACTOR = "actor ~ '^operator:[A-Za-z0-9._@-]+$'"


class RiskConfigurationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_configurations"

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT"), unique=True
    )
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskConfigurationVersionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_configuration_versions"
    __table_args__ = (
        UniqueConstraint("configuration_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(version = 1) = (previous_version_id IS NULL)", name="previous_iff_not_first"
        ),
        CheckConstraint("(version = 1) = (change_kind = 'INITIAL')", name="initial_is_first"),
        # Only an authorized operator writes risk configuration: never a strategy, model, LLM
        # or worker (spec 3.6, 14.7).
        CheckConstraint(OPERATOR_ACTOR, name="operator_actor"),
        CheckConstraint("length(btrim(reason)) >= 10", name="reason_given"),
        Index(None, "portfolio_account_id", "version"),
    )

    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_configurations.id", ondelete="RESTRICT")
    )
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_configuration_versions.id", ondelete="RESTRICT")
    )
    change_kind: Mapped[ConfigChangeKind] = mapped_column(
        str_enum(ConfigChangeKind, "risk_config_change_kind")
    )
    hard_limits: Mapped[dict[str, Any]]
    preferences: Mapped[dict[str, Any]]
    hard_limits_sha256: Mapped[str] = mapped_column(String(64))
    preferences_sha256: Mapped[str] = mapped_column(String(64))
    # Previous and new values of every changed field (all fields for the first version).
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskAssetClassificationRecord(Base):
    __tablename__ = "risk_asset_classifications"
    __table_args__ = (
        UniqueConstraint("asset_id", "valid_from"),
        CheckConstraint("sector IS NOT NULL OR cluster IS NOT NULL", name="classifies"),
        CheckConstraint("sector IS NULL OR sector ~ '^[A-Z0-9_]{2,48}$'", name="sector_format"),
        CheckConstraint("cluster IS NULL OR cluster ~ '^[A-Z0-9_]{2,48}$'", name="cluster_format"),
        CheckConstraint(
            "source = 'TEST_FIXTURE' OR " + OPERATOR_ACTOR.replace("actor", "created_by"),
            name="operator_or_fixture",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    sector: Mapped[str | None] = mapped_column(String(48))
    cluster: Mapped[str | None] = mapped_column(String(48))
    valid_from: Mapped[datetime]
    source: Mapped[ClassificationSource] = mapped_column(
        str_enum(ClassificationSource, "risk_classification_source")
    )
    provenance: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskExposureSnapshotRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_exposure_snapshots"
    __table_args__ = (
        CheckConstraint("ledger_sequence >= 0", name="sequence_non_negative"),
        CheckConstraint(
            "gross_exposure = long_exposure + short_exposure", name="gross_is_long_plus_short"
        ),
        CheckConstraint(
            "net_exposure = long_exposure - short_exposure", name="net_is_long_minus_short"
        ),
        # Active risk reservations (Phase 6C); 6B required 0 ("no_reservations_before_6c").
        CheckConstraint("pending_exposure >= 0", name="pending_non_negative"),
        Index(None, "portfolio_account_id", "as_of"),
        Index(None, "portfolio_account_id", "mark_status", "as_of"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    as_of: Mapped[datetime]
    ledger_sequence: Mapped[int] = mapped_column(BigInteger)
    trigger: Mapped[SnapshotTrigger] = mapped_column(
        str_enum(SnapshotTrigger, "risk_snapshot_trigger")
    )
    mark_status: Mapped[MarkStatus] = mapped_column(str_enum(MarkStatus, "risk_mark_status"))
    functional_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    equity: Mapped[Decimal] = mapped_column(AMOUNT)
    contributed_capital: Mapped[Decimal] = mapped_column(AMOUNT)
    cash: Mapped[Decimal] = mapped_column(AMOUNT)
    available_capital: Mapped[Decimal] = mapped_column(AMOUNT)
    long_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    short_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    gross_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    net_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    margin_used: Mapped[Decimal] = mapped_column(AMOUNT)
    pending_exposure: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    open_positions: Mapped[int] = mapped_column(Integer)
    daily_loss: Mapped[Decimal | None] = mapped_column(AMOUNT)
    daily_baseline: Mapped[Decimal | None] = mapped_column(AMOUNT)
    weekly_loss: Mapped[Decimal | None] = mapped_column(AMOUNT)
    weekly_baseline: Mapped[Decimal | None] = mapped_column(AMOUNT)
    peak_equity: Mapped[Decimal | None] = mapped_column(AMOUNT)
    drawdown: Mapped[Decimal | None] = mapped_column(AMOUNT)
    available_risk_budget: Mapped[Decimal | None] = mapped_column(AMOUNT)
    # Per asset, asset class, sector and cluster; per-position prices with provenance.
    breakdown: Mapped[dict[str, Any]]
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_configuration_versions.id", ondelete="RESTRICT")
    )
    engine_version: Mapped[str] = mapped_column(String(16))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskPeriodBaselineRecord(Base):
    __tablename__ = "risk_period_baselines"
    __table_args__ = (UniqueConstraint("portfolio_account_id", "period_kind", "period_start"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    period_kind: Mapped[PeriodKind] = mapped_column(str_enum(PeriodKind, "risk_period_kind"))
    period_label: Mapped[str] = mapped_column(String(16))
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    baseline_equity: Mapped[Decimal] = mapped_column(AMOUNT)
    baseline_contributed_capital: Mapped[Decimal] = mapped_column(AMOUNT)
    basis: Mapped[BaselineBasis] = mapped_column(str_enum(BaselineBasis, "risk_baseline_basis"))
    observed_at: Mapped[datetime]
    source_kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    timezone_policy: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskAssessmentRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_assessments"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "request_key"),
        CheckConstraint(
            "result <> 'PASS_FOR_RESERVATION' OR (permitted_quantity > 0 AND risk_budget > 0 "
            "AND potential_loss > 0 AND potential_loss <= risk_budget AND equity > 0 "
            "AND config_version_id IS NOT NULL)",
            name="pass_is_sized_within_budget",
        ),
        CheckConstraint(
            "result = 'PASS_FOR_RESERVATION' OR permitted_quantity IS NULL",
            name="only_pass_has_quantity",
        ),
        CheckConstraint("valid_until > assessed_at", name="valid_after_assessment"),
        CheckConstraint("entry > 0", name="entry_positive"),
        Index(None, "portfolio_account_id", "assessed_at"),
        Index(None, "result", "assessed_at"),
        Index(None, "asset_id", "assessed_at"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    # Optional idempotency key: the same key returns the stored assessment.
    request_key: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[ProposalSource] = mapped_column(str_enum(ProposalSource, "risk_proposal_source"))
    strategy_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    direction: Mapped[TradeDirection] = mapped_column(
        str_enum(TradeDirection, "risk_trade_direction")
    )
    entry: Mapped[Decimal] = mapped_column(PRICE)
    stop: Mapped[Decimal | None] = mapped_column(PRICE)
    target: Mapped[Decimal | None] = mapped_column(PRICE)
    requested_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    result: Mapped[AssessmentResult] = mapped_column(
        str_enum(AssessmentResult, "risk_assessment_result")
    )
    permitted_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    functional_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    equity: Mapped[Decimal] = mapped_column(AMOUNT)
    risk_budget: Mapped[Decimal | None] = mapped_column(AMOUNT)
    potential_loss: Mapped[Decimal | None] = mapped_column(AMOUNT)
    notional: Mapped[Decimal | None] = mapped_column(AMOUNT)
    estimated_costs: Mapped[Decimal | None] = mapped_column(AMOUNT)
    binding_constraint: Mapped[str | None] = mapped_column(String(48))
    inputs: Mapped[dict[str, Any]]
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    calculations: Mapped[dict[str, Any]]
    exposure_after: Mapped[dict[str, Any]]
    exposure_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_exposure_snapshots.id", ondelete="RESTRICT")
    )
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_configuration_versions.id", ondelete="RESTRICT")
    )
    ledger_sequence: Mapped[int] = mapped_column(BigInteger)
    assessed_at: Mapped[datetime]
    valid_until: Mapped[datetime]
    engine_version: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    # The writing transaction: reasons can only be added in it (guard trigger).
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskAssessmentReasonRecord(Base):
    __tablename__ = "risk_assessment_reasons"
    __table_args__ = (
        UniqueConstraint("assessment_id", "ordinal"),
        Index(None, "reason_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    check_code: Mapped[str] = mapped_column(String(48))
    outcome: Mapped[CheckOutcome] = mapped_column(str_enum(CheckOutcome, "risk_check_outcome"))
    reason_code: Mapped[str | None] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    measured: Mapped[Decimal | None]
    limit_value: Mapped[Decimal | None]
    unit: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")


class RiskCircuitBreakerEventRecord(Base):
    __tablename__ = "risk_circuit_breaker_events"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "breaker_type", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(event = 'TRIPPED' AND previous_status = 'CLEAR' AND new_status = 'TRIPPED') OR "
            "(event = 'RESET' AND previous_status = 'TRIPPED' AND new_status = 'CLEAR')",
            name="valid_transition",
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        CheckConstraint(
            "actor_kind = 'OPERATOR' OR actor !~ '^operator:'", name="operator_prefix_matches"
        ),
        CheckConstraint(
            "actor_kind <> 'OPERATOR' OR " + OPERATOR_ACTOR, name="operator_actor_format"
        ),
        # Only an operator resets a breaker, except the data-quality breaker, whose recovery
        # is automatic once every input is fresh again. Only an operator engages the kill
        # switch.
        CheckConstraint(
            "event <> 'RESET' OR actor_kind = 'OPERATOR' OR breaker_type = 'DATA_QUALITY'",
            name="manual_reset",
        ),
        CheckConstraint(
            "breaker_type <> 'KILL_SWITCH' OR actor_kind = 'OPERATOR'", name="kill_switch_operator"
        ),
        Index(None, "portfolio_account_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    breaker_type: Mapped[BreakerType] = mapped_column(str_enum(BreakerType, "risk_breaker_type"))
    sequence: Mapped[int] = mapped_column(Integer)
    event: Mapped[BreakerEvent] = mapped_column(str_enum(BreakerEvent, "risk_breaker_event"))
    previous_status: Mapped[BreakerStatus] = mapped_column(
        str_enum(BreakerStatus, "risk_breaker_previous_status")
    )
    new_status: Mapped[BreakerStatus] = mapped_column(
        str_enum(BreakerStatus, "risk_breaker_new_status")
    )
    occurred_at: Mapped[datetime]
    actor_kind: Mapped[ActorKind] = mapped_column(str_enum(ActorKind, "risk_actor_kind"))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    measurements: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    response: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_configuration_versions.id", ondelete="RESTRICT")
    )
    exposure_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_exposure_snapshots.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RiskEventRecord(Base):
    __tablename__ = "risk_events"
    __table_args__ = (
        Index(None, "portfolio_account_id", "occurred_at"),
        Index(None, "event_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    portfolio_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    event_type: Mapped[RiskEventType] = mapped_column(str_enum(RiskEventType, "risk_event_type"))
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "risk_event_severity"))
    occurred_at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(128))
    subject_kind: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "RiskAssessmentReasonRecord",
    "RiskAssessmentRecord",
    "RiskAssetClassificationRecord",
    "RiskCircuitBreakerEventRecord",
    "RiskConfigurationRecord",
    "RiskConfigurationVersionRecord",
    "RiskEventRecord",
    "RiskExposureSnapshotRecord",
    "RiskPeriodBaselineRecord",
]

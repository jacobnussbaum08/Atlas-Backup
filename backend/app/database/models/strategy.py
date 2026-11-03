"""Strategy Laboratory storage (Phase 4A).

* ``feature_definitions``: the central feature registry as registered (append-only; a
  definition never changes under the same name and version).
* ``strategies``: strategy identities (id and family).
* ``strategy_configurations``: immutable parameter sets, content-addressed per strategy.
* ``strategy_versions``: immutable (logic version, configuration) pairs, numbered per
  strategy; the newest is current. Changing parameters or logic adds a version.
* ``strategy_status_events``: enable/disable history; the newest event is the status
  (no event: disabled).
* ``strategy_feature_snapshots``: the point-in-time feature values an evaluation used,
  content-addressed and shared between evaluations with identical inputs.
* ``strategy_evaluations``: one attempt to evaluate a strategy version for an asset,
  timeframe and trigger. Its result is written once (RUNNING -> terminal, enforced by a
  trigger); a failed attempt stays and a retry is a new row.
* ``strategy_evaluation_snapshots``: evaluation -> snapshots used.
* ``strategy_evaluation_errors``: failures, by stage.
* ``strategy_proposals``: standardized proposals (spec 11.7); hypotheses, not trades.
* ``strategy_proposal_events``: expiry and supersession, recorded when Atlas noticed them.
* ``strategy_proposal_conflicts`` (Phase 4B): overlapping opposite-direction proposals for
  the same asset, recorded for the Phase 5 coordinator (never resolved here).

Every table except ``strategy_evaluations`` is append-only (triggers, migration 0009).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.data_engine.enums import PriceAdjustment
from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.feature_engine.registry import DefinitionStatus, FeatureSource, ValueKind
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import (
    ConflictKind,
    Direction,
    ErrorStage,
    EvaluationMode,
    EvaluationStatus,
    ProposalEventKind,
    SignalState,
    StrategyFamily,
    StrengthKind,
    TriggerKind,
)

TIMEFRAME = "^(1Day|1Week)$"
STRATEGY_ID = "^[a-z][a-z0-9_]{2,47}$"
PRICE = Numeric(20, 8)
STRENGTH = Numeric(5, 4)
_STRATEGY_FK = "strategies.id"


class FeatureDefinitionRecord(Base):
    __tablename__ = "feature_definitions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(16))
    source: Mapped[FeatureSource] = mapped_column(str_enum(FeatureSource, "feature_source"))
    description: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    timeframes: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    required_data: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    availability: Mapped[str] = mapped_column(Text)
    value_kind: Mapped[ValueKind] = mapped_column(str_enum(ValueKind, "feature_value_kind"))
    status: Mapped[DefinitionStatus] = mapped_column(
        str_enum(DefinitionStatus, "feature_definition_status")
    )
    window_rule: Mapped[str | None] = mapped_column(String(64))
    definition_sha256: Mapped[str] = mapped_column(String(64))
    registry_version: Mapped[str] = mapped_column(String(32))
    registered_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyRecord(Base):
    __tablename__ = "strategies"
    __table_args__ = (CheckConstraint(f"id ~ '{STRATEGY_ID}'", name="id_format"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    family: Mapped[StrategyFamily] = mapped_column(str_enum(StrategyFamily, "strategy_family"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyConfigurationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "strategy_configurations"
    __table_args__ = (UniqueConstraint("strategy_id", "parameters_sha256"),)

    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    # Validated parameters as JSON (decimals as strings), every field explicit.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters_sha256: Mapped[str] = mapped_column(String(64))
    # Hash of the parameter schema the configuration was validated against.
    schema_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyVersionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    logic_version: Mapped[str] = mapped_column(String(64))
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_configurations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    implemented: Mapped[bool] = mapped_column(Boolean)
    supported_asset_classes: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    supported_timeframes: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Timeframe -> canonical feature keys (required and optional) under this version.
    required_features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    feature_registry_version: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyStatusEventRecord(Base):
    __tablename__ = "strategy_status_events"
    __table_args__ = (Index(None, "strategy_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    enabled: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyFeatureSnapshotRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "strategy_feature_snapshots"
    __table_args__ = (
        UniqueConstraint("content_sha256"),
        Index(None, "asset_id", "timeframe", "data_cutoff"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("known_at >= data_cutoff", name="known_after_cutoff"),
        CheckConstraint(
            "basis <> 'RECORDED' OR known_at = data_cutoff", name="recorded_known_at_cutoff"
        ),
        CheckConstraint(
            "basis <> 'RECORDED' OR inputs_available_at IS NULL "
            "OR inputs_available_at <= data_cutoff",
            name="recorded_inputs_known",
        ),
        CheckConstraint(
            "last_period_event_at IS NULL OR last_period_event_at <= data_cutoff",
            name="no_future_period",
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    timeframe: Mapped[str] = mapped_column(String(16))
    data_cutoff: Mapped[datetime]
    known_at: Mapped[datetime]
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    # The bar series the bar features were computed from.
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    adjustment: Mapped[PriceAdjustment] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    feature_registry_version: Mapped[str] = mapped_column(String(32))
    # Canonical feature key -> value, status, reason, availability and inputs.
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    feature_count: Mapped[int] = mapped_column(Integer)
    valid_count: Mapped[int] = mapped_column(Integer)
    inputs_available_at: Mapped[datetime | None]
    last_period_event_at: Mapped[datetime | None]
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StrategyEvaluationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "strategy_evaluations"
    __table_args__ = (
        # At most one non-failed evaluation per key; failed attempts stay as history.
        Index(
            "uq_strategy_evaluations_live_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status <> 'FAILED'"),
        ),
        UniqueConstraint("idempotency_key", "attempt"),
        Index(None, "strategy_id", "started_at"),
        Index(None, "asset_id", "started_at"),
        Index(None, "status", "lease_expires_at"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name="finished_iff_done"),
        CheckConstraint("known_at >= data_cutoff", name="known_after_cutoff"),
        CheckConstraint(
            "basis <> 'RECORDED' OR known_at = data_cutoff", name="recorded_known_at_cutoff"
        ),
        CheckConstraint(
            "(status = 'PROPOSED') = (proposals_count > 0)", name="proposed_iff_proposals"
        ),
        CheckConstraint(
            "(status = 'RUNNING') = (signal_state IS NULL)", name="signal_state_iff_done"
        ),
        CheckConstraint(
            "(status = 'PROPOSED') = (signal_state = 'SIGNAL')", name="signal_iff_proposed"
        ),
        Index(None, "signal_state", "started_at"),
    )

    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    timeframe: Mapped[str] = mapped_column(String(16))
    mode: Mapped[EvaluationMode] = mapped_column(str_enum(EvaluationMode, "evaluation_mode"))
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    trigger: Mapped[TriggerKind] = mapped_column(str_enum(TriggerKind, "evaluation_trigger"))
    trigger_ref: Mapped[str] = mapped_column(String(128))
    # sha256 of (version, asset, timeframe, mode, trigger, trigger_ref): the same update
    # never produces two results.
    idempotency_key: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[EvaluationStatus] = mapped_column(
        str_enum(EvaluationStatus, "evaluation_status")
    )
    data_cutoff: Mapped[datetime]
    known_at: Mapped[datetime]
    worker_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime]
    lease_expires_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    proposals_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # Required features that were not VALID (SKIPPED), and other outcome notes.
    skip_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    notes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Phase 4B: what the evaluation found, why nothing was proposed, the conditions the
    # strategy evaluated and the asset's point-in-time eligibility.
    signal_state: Mapped[SignalState | None] = mapped_column(
        str_enum(SignalState, "strategy_signal_state")
    )
    signal_reason: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    eligibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class StrategyEvaluationSnapshotRecord(Base):
    __tablename__ = "strategy_evaluation_snapshots"

    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_evaluations.id", ondelete="RESTRICT"), primary_key=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_feature_snapshots.id", ondelete="RESTRICT"), primary_key=True
    )


class StrategyEvaluationErrorRecord(Base):
    __tablename__ = "strategy_evaluation_errors"
    __table_args__ = (Index(None, "evaluation_id"), Index(None, "strategy_id", "occurred_at"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_evaluations.id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    stage: Mapped[ErrorStage] = mapped_column(str_enum(ErrorStage, "evaluation_error_stage"))
    error_type: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)  # redacted
    occurred_at: Mapped[datetime]


class StrategyProposalRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "strategy_proposals"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "direction"),
        # One proposal per strategy, asset, timeframe, direction, mode, basis and event.
        UniqueConstraint(
            "strategy_id",
            "asset_id",
            "timeframe",
            "direction",
            "mode",
            "basis",
            "evaluation_event",
            name="uq_strategy_proposals_event",
        ),
        Index(None, "strategy_id", "signal_at"),
        Index(None, "asset_id", "signal_at"),
        Index(None, "expires_at"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("data_cutoff <= signal_at", name="cutoff_before_signal"),
        CheckConstraint("expires_at > signal_at", name="expires_after_signal"),
        CheckConstraint("reference_entry > 0", name="entry_positive"),
        CheckConstraint("signal_strength >= 0 AND signal_strength <= 1", name="strength_range"),
        CheckConstraint("holding_horizon_periods >= 1", name="horizon_positive"),
        CheckConstraint("cardinality(feature_snapshot_ids) >= 1", name="has_snapshots"),
        CheckConstraint(
            "invalidation_level IS NULL OR (direction = 'LONG' AND invalidation_level < "
            "reference_entry) OR (direction = 'SHORT' AND invalidation_level > reference_entry)",
            name="invalidation_side",
        ),
        CheckConstraint(
            "target_reference IS NULL OR (direction = 'LONG' AND target_reference > "
            "reference_entry) OR (direction = 'SHORT' AND target_reference < reference_entry)",
            name="target_side",
        ),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_evaluations.id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[str] = mapped_column(ForeignKey(_STRATEGY_FK, ondelete="RESTRICT"))
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    market: Mapped[str] = mapped_column(String(16))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction, "proposal_direction"))
    timeframe: Mapped[str] = mapped_column(String(16))
    mode: Mapped[EvaluationMode] = mapped_column(str_enum(EvaluationMode, "evaluation_mode"))
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    signal_at: Mapped[datetime]
    data_cutoff: Mapped[datetime]
    feature_snapshot_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    reference_entry: Mapped[Decimal] = mapped_column(PRICE)
    invalidation_level: Mapped[Decimal | None] = mapped_column(PRICE)
    target_reference: Mapped[Decimal | None] = mapped_column(PRICE)
    holding_horizon_periods: Mapped[int] = mapped_column(Integer)
    signal_strength: Mapped[Decimal] = mapped_column(STRENGTH)
    strength_kind: Mapped[StrengthKind] = mapped_column(str_enum(StrengthKind, "strength_kind"))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reasoning: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Phase 4B: "<trigger>:<trigger_ref>" of the evaluation, and how the levels were derived.
    evaluation_event: Mapped[str] = mapped_column(String(160))
    levels: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class StrategyProposalEventRecord(Base):
    __tablename__ = "strategy_proposal_events"
    __table_args__ = (
        UniqueConstraint("proposal_id", "kind"),
        CheckConstraint(
            "(kind = 'SUPERSEDED') = (superseded_by IS NOT NULL)", name="superseded_by_iff_kind"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    kind: Mapped[ProposalEventKind] = mapped_column(
        str_enum(ProposalEventKind, "proposal_event_kind")
    )
    # When it happened (expiry time, or the superseding signal) and when Atlas recorded it.
    occurred_at: Mapped[datetime]
    recorded_at: Mapped[datetime]
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )


class StrategyProposalConflictRecord(Base):
    """Two overlapping proposals for the same asset in opposite directions (Phase 4B).

    Recorded when the second one is stored; the pair is ordered (``proposal_a_id`` <
    ``proposal_b_id``) so it is recorded once. A conflict is information for the Phase 5
    coordinator, not a decision: neither proposal is changed.
    """

    __tablename__ = "strategy_proposal_conflicts"
    __table_args__ = (
        UniqueConstraint("proposal_a_id", "proposal_b_id"),
        Index(None, "asset_id", "overlap_start"),
        Index(None, "proposal_b_id"),
        CheckConstraint("proposal_a_id < proposal_b_id", name="ordered_pair"),
        CheckConstraint("overlap_end > overlap_start", name="overlap_positive"),
        CheckConstraint(
            "detected_by = proposal_a_id OR detected_by = proposal_b_id", name="detected_by_member"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    proposal_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    proposal_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    kind: Mapped[ConflictKind] = mapped_column(str_enum(ConflictKind, "proposal_conflict_kind"))
    mode: Mapped[EvaluationMode] = mapped_column(str_enum(EvaluationMode, "evaluation_mode"))
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    # When both proposals were active at once.
    overlap_start: Mapped[datetime]
    overlap_end: Mapped[datetime]
    detected_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # the newer proposal
    detected_at: Mapped[datetime]


__all__ = [
    "FeatureDefinitionRecord",
    "StrategyConfigurationRecord",
    "StrategyEvaluationErrorRecord",
    "StrategyEvaluationRecord",
    "StrategyEvaluationSnapshotRecord",
    "StrategyFeatureSnapshotRecord",
    "StrategyProposalConflictRecord",
    "StrategyProposalEventRecord",
    "StrategyProposalRecord",
    "StrategyRecord",
    "StrategyStatusEventRecord",
    "StrategyVersionRecord",
]

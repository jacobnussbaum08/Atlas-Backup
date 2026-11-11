"""Validation laboratory and champion/challenger governance (Phase 8B, migration 0019).

* ``validation_experiments`` hold an experiment's configuration; never changed or deleted.
* ``validation_runs``: one execution of an experiment with its resolved base configuration.
  Status moves only QUEUED -> RUNNING -> COMPLETED | FAILED; configuration columns never
  change; a final run never changes (database trigger).
* Run results (``validation_trials``, ``validation_folds``, ``validation_robustness_results``,
  ``validation_segment_results``) are insert-only and only while their run is RUNNING.
* ``validation_holdout_evaluations``: the only reads of a run's untouched final period, each
  numbered; insert-only, and only for a COMPLETED run.
* ``validation_comparisons``, ``learning_candidates``, ``learning_candidate_events``,
  ``learning_approvals``, ``learning_monitoring_rules``, ``learning_drift_measurements`` and
  ``learning_degradation_alerts`` are append-only: history is never rewritten.

Everything validated here is a simulation over stored historical data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.learning.enums import (
    ActorKind,
    AlertAction,
    CandidateStatus,
    Decision,
    ExperimentKind,
    MeasurementStatus,
    MonitorMetric,
    ResultStatus,
    RobustnessMethod,
    RuleAction,
    RunStatus,
    Sample,
    SearchMethod,
    SegmentKind,
    SubjectKind,
    TrialSource,
    TrialStatus,
    Verdict,
)

_RUN = "validation_runs.id"
_CANDIDATE = "learning_candidates.id"
SHA = "~ '^[0-9a-f]{64}$'"


def _run_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey(_RUN, ondelete="RESTRICT"))


class ValidationExperimentRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_experiments"
    __table_args__ = (
        CheckConstraint(f"config_sha256 {SHA}", name="config_sha_format"),
        CheckConstraint("simulated", name="always_simulated"),
        Index(None, "created_at"),
    )

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[ExperimentKind] = mapped_column(
        str_enum(ExperimentKind, "validation_experiment_kind")
    )
    simulated: Mapped[bool] = mapped_column(Boolean, server_default="true")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String(64))
    strategy_ids: Mapped[list[str]] = mapped_column(ARRAY(String(48)))
    search_method: Mapped[SearchMethod] = mapped_column(
        str_enum(SearchMethod, "validation_search_method")
    )
    max_trials: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_runs"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="date_order"),
        CheckConstraint(f"base_config_sha256 {SHA}", name="base_config_sha_format"),
        CheckConstraint(
            "(status = 'COMPLETED') = (result_sha256 IS NOT NULL)", name="result_when_completed"
        ),
        CheckConstraint("status <> 'FAILED' OR error_message IS NOT NULL", name="failed_has_error"),
        Index(None, "created_at"),
        Index(None, "status"),
        Index(None, "experiment_id"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("validation_experiments.id", ondelete="RESTRICT")
    )
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "validation_run_status"))
    base_config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    base_config_sha256: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(16))
    code_version: Mapped[str] = mapped_column(String(80))
    seed: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    data_known_at: Mapped[datetime]
    requested_by: Mapped[str] = mapped_column(String(128))
    reproduction_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(_RUN, ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Progress and outcome (the only columns that change, and only until the run is final).
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    prior_trials: Mapped[int | None] = mapped_column(Integer)
    layout: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verdict: Mapped[Verdict | None] = mapped_column(str_enum(Verdict, "validation_verdict"))
    findings: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    holdout_start: Mapped[date | None] = mapped_column(Date)
    holdout_end: Mapped[date | None] = mapped_column(Date)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    reproduced_identical: Mapped[bool | None] = mapped_column(Boolean)
    error_message: Mapped[str | None] = mapped_column(Text)


class ValidationTrialRecord(Base):
    __tablename__ = "validation_trials"
    __table_args__ = (
        UniqueConstraint("run_id", "trial_number"),
        CheckConstraint(f"parameters_sha256 {SHA}", name="parameters_sha_format"),
        CheckConstraint("status <> 'FAILED' OR error IS NOT NULL", name="failed_has_error"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = _run_fk()
    trial_number: Mapped[int] = mapped_column(Integer)
    strategy_id: Mapped[str] = mapped_column(String(200))
    source: Mapped[TrialSource] = mapped_column(str_enum(TrialSource, "validation_trial_source"))
    status: Mapped[TrialStatus] = mapped_column(str_enum(TrialStatus, "validation_trial_status"))
    selectable: Mapped[bool] = mapped_column(Boolean)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters_sha256: Mapped[str] = mapped_column(String(64))
    config_sha256: Mapped[str | None] = mapped_column(String(64))
    dataset_sha256: Mapped[str | None] = mapped_column(String(64))
    development: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # [[session, net daily return], ...] of the development replay (PBO/CPCV inputs).
    development_returns: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationFoldRecord(Base):
    __tablename__ = "validation_folds"
    __table_args__ = (
        UniqueConstraint("run_id", "fold_index"),
        CheckConstraint("test_start > train_end", name="test_after_training"),
        CheckConstraint(
            "validation_start IS NULL OR (validation_start > train_end AND test_start > "
            "validation_end)",
            name="validation_between",
        ),
        CheckConstraint("test_end >= test_start", name="test_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = _run_fk()
    fold_index: Mapped[int] = mapped_column(Integer)
    refit: Mapped[bool] = mapped_column(Boolean)
    train_start: Mapped[date] = mapped_column(Date)
    train_end: Mapped[date] = mapped_column(Date)
    validation_start: Mapped[date | None] = mapped_column(Date)
    validation_end: Mapped[date | None] = mapped_column(Date)
    test_start: Mapped[date] = mapped_column(Date)
    test_end: Mapped[date] = mapped_column(Date)
    embargo_sessions: Mapped[int] = mapped_column(Integer)
    selected_trial: Mapped[int] = mapped_column(Integer)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB)
    in_sample: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    out_of_sample: Mapped[dict[str, Any]] = mapped_column(JSONB)
    champion_out_of_sample: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # [[session, net daily return, equity], ...] of the fresh test-window replay.
    oos_series: Mapped[list[Any]] = mapped_column(JSONB)
    champion_series: Mapped[list[Any] | None] = mapped_column(JSONB)
    oos_config_sha256: Mapped[str] = mapped_column(String(64))
    oos_dataset_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationRobustnessRecord(Base):
    __tablename__ = "validation_robustness_results"
    __table_args__ = (Index(None, "run_id", "method"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = _run_fk()
    method: Mapped[RobustnessMethod] = mapped_column(
        str_enum(RobustnessMethod, "validation_robustness_method")
    )
    scope: Mapped[str] = mapped_column(String(200))
    status: Mapped[ResultStatus] = mapped_column(str_enum(ResultStatus, "validation_result_status"))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    assumptions: Mapped[list[str]] = mapped_column(JSONB)
    limitations: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationSegmentRecord(Base):
    __tablename__ = "validation_segment_results"
    __table_args__ = (Index(None, "run_id", "segment_kind"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = _run_fk()
    sample: Mapped[Sample] = mapped_column(str_enum(Sample, "validation_sample"))
    segment_kind: Mapped[SegmentKind] = mapped_column(
        str_enum(SegmentKind, "validation_segment_kind")
    )
    segment_value: Mapped[str] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[ResultStatus] = mapped_column(str_enum(ResultStatus, "validation_result_status"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationHoldoutRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_holdout_evaluations"
    __table_args__ = (
        CheckConstraint("holdout_end >= holdout_start", name="date_order"),
        CheckConstraint("access_number >= 1", name="access_positive"),
        Index(None, "run_id"),
    )

    run_id: Mapped[uuid.UUID] = _run_fk()
    strategy_ids: Mapped[list[str]] = mapped_column(ARRAY(String(48)))
    holdout_start: Mapped[date] = mapped_column(Date)
    holdout_end: Mapped[date] = mapped_column(Date)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters_sha256: Mapped[str] = mapped_column(String(64))
    # The n-th evaluation of any configuration of these strategies on an overlapping
    # holdout: above 1, the holdout is no longer untouched for them.
    access_number: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String(64))
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ValidationComparisonRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "validation_comparisons"
    __table_args__ = (Index(None, "created_at"),)

    kind: Mapped[SubjectKind] = mapped_column(str_enum(SubjectKind, "learning_subject_kind"))
    title: Mapped[str] = mapped_column(String(200))
    subjects: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------------- governance
class LearningCandidateRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_candidates"
    __table_args__ = (
        CheckConstraint(
            "(subject_kind = 'STRATEGY') = (strategy_id IS NOT NULL AND parameters IS NOT NULL "
            "AND model_version_id IS NULL)",
            name="strategy_subject",
        ),
        CheckConstraint(
            "(subject_kind = 'MODEL') = (model_version_id IS NOT NULL AND strategy_id IS NULL)",
            name="model_subject",
        ),
        Index(None, "subject_kind", "subject_key"),
    )

    subject_kind: Mapped[SubjectKind] = mapped_column(
        str_enum(SubjectKind, "learning_subject_kind")
    )
    subject_key: Mapped[str] = mapped_column(String(128))  # strategy id or model name
    strategy_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="RESTRICT")
    )
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parameters_sha256: Mapped[str | None] = mapped_column(String(64))
    base_strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ml_model_versions.id", ondelete="RESTRICT")
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(_RUN, ondelete="RESTRICT"))
    champion_at_creation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    notes: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LearningCandidateEventRecord(Base):
    __tablename__ = "learning_candidate_events"
    __table_args__ = (
        UniqueConstraint("previous_event_id"),
        Index(
            "uq_learning_candidate_events_first",
            "candidate_id",
            unique=True,
            postgresql_where=text("previous_event_id IS NULL"),
        ),
        CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_CANDIDATE, ondelete="RESTRICT"))
    previous_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_candidate_events.id", ondelete="RESTRICT")
    )
    from_status: Mapped[CandidateStatus | None] = mapped_column(
        str_enum(CandidateStatus, "learning_candidate_from_status")
    )
    to_status: Mapped[CandidateStatus] = mapped_column(
        str_enum(CandidateStatus, "learning_candidate_to_status")
    )
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128))
    actor_kind: Mapped[ActorKind] = mapped_column(str_enum(ActorKind, "learning_actor_kind"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LearningApprovalRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_approvals"
    __table_args__ = (
        CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
        Index(None, "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_CANDIDATE, ondelete="RESTRICT"))
    event_id: Mapped[int] = mapped_column(
        ForeignKey("learning_candidate_events.id", ondelete="RESTRICT")
    )
    decision: Mapped[Decision] = mapped_column(str_enum(Decision, "learning_decision"))
    approver: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    replaced: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ml_model_versions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LearningMonitoringRuleRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_monitoring_rules"
    __table_args__ = (
        UniqueConstraint("subject_kind", "subject_key", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "action <> 'SUSPEND' OR subject_kind = 'STRATEGY'", name="suspend_strategies_only"
        ),
        CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
    )

    subject_kind: Mapped[SubjectKind] = mapped_column(
        str_enum(SubjectKind, "learning_subject_kind")
    )
    subject_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    rule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    rule_sha256: Mapped[str] = mapped_column(String(64))
    action: Mapped[RuleAction] = mapped_column(str_enum(RuleAction, "learning_rule_action"))
    active: Mapped[bool] = mapped_column(Boolean)
    approved_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LearningDriftMeasurementRecord(Base):
    __tablename__ = "learning_drift_measurements"
    __table_args__ = (
        Index(None, "subject_kind", "subject_key", "measured_at"),
        Index(None, "monitor_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    monitor_run_id: Mapped[uuid.UUID]
    subject_kind: Mapped[SubjectKind] = mapped_column(
        str_enum(SubjectKind, "learning_subject_kind")
    )
    subject_key: Mapped[str] = mapped_column(String(128))
    metric: Mapped[MonitorMetric] = mapped_column(
        str_enum(MonitorMetric, "learning_monitor_metric")
    )
    feature: Mapped[str | None] = mapped_column(String(200))
    measured_at: Mapped[datetime]
    reference: Mapped[dict[str, Any]] = mapped_column(JSONB)
    current: Mapped[dict[str, Any]] = mapped_column(JSONB)
    value: Mapped[Decimal | None]
    p_value: Mapped[Decimal | None]
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB)
    n_reference: Mapped[int] = mapped_column(Integer)
    n_current: Mapped[int] = mapped_column(Integer)
    status: Mapped[MeasurementStatus] = mapped_column(
        str_enum(MeasurementStatus, "learning_measurement_status")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learning_monitoring_rules.id", ondelete="RESTRICT")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LearningDegradationAlertRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_degradation_alerts"
    __table_args__ = (Index(None, "subject_kind", "subject_key", "created_at"),)

    subject_kind: Mapped[SubjectKind] = mapped_column(
        str_enum(SubjectKind, "learning_subject_kind")
    )
    subject_key: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    metric: Mapped[MonitorMetric] = mapped_column(
        str_enum(MonitorMetric, "learning_monitor_metric")
    )
    message: Mapped[str] = mapped_column(Text)
    measurement_id: Mapped[int] = mapped_column(
        ForeignKey("learning_drift_measurements.id", ondelete="RESTRICT")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learning_monitoring_rules.id", ondelete="RESTRICT")
    )
    action: Mapped[AlertAction] = mapped_column(str_enum(AlertAction, "learning_alert_action"))
    action_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "LearningApprovalRecord",
    "LearningCandidateEventRecord",
    "LearningCandidateRecord",
    "LearningDegradationAlertRecord",
    "LearningDriftMeasurementRecord",
    "LearningMonitoringRuleRecord",
    "ValidationComparisonRecord",
    "ValidationExperimentRecord",
    "ValidationFoldRecord",
    "ValidationHoldoutRecord",
    "ValidationRobustnessRecord",
    "ValidationRunRecord",
    "ValidationSegmentRecord",
    "ValidationTrialRecord",
]

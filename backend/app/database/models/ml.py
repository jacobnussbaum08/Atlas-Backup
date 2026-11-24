"""Machine-learning engine storage (Phase 5A).

* ``ml_datasets``: dataset identities (a name and a description).
* ``ml_dataset_versions``: immutable built datasets: the specification, feature schema and
  target definition, the data snapshot time, statistics, provenance and warnings.
* ``ml_dataset_observations``: one row per observation of a version: decision time, asset,
  direction, strategy version, point-in-time feature values with their availability, and
  the label with its maturity.
* ``ml_training_runs``: one training attempt. Its outcome is written once (RUNNING ->
  SUCCEEDED/FAILED, enforced by a trigger); a failed run stays as history.
* ``ml_model_artifacts``: serialized models with their sha256 (the file lives under
  ``ML_ARTIFACT_DIR``; the hash is checked before every load).
* ``ml_model_versions``: the registry. A new model is a new version; nothing is replaced.
* ``ml_model_status_events``: lifecycle history; the newest event is the status. Every
  transition names its actor: nothing is promoted automatically.
* ``ml_model_evaluations``: metrics per split, fold and segment.
* ``ml_calibration_results``: calibration fitted on validation data and its evidence.
* ``ml_predictions``: inference results with the exact inputs, model version and validity.

Every table except ``ml_training_runs`` is append-only (triggers, migration 0011).
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.enums import (
    CalibrationMethod,
    DataOrigin,
    LabelStatus,
    LifecycleStatus,
    ModelType,
    ObservationSource,
    OutputKind,
    PredictionMode,
    PredictionValidity,
    RunStatus,
    SegmentKind,
    SplitKind,
    TargetKind,
    TaskKind,
    ValidationStatus,
)
from app.strategies.enums import Direction

TIMEFRAME = "^(1Day|1Week)$"
NAME = "^[a-z][a-z0-9_.-]{2,63}$"
_DATASET_VERSION_FK = "ml_dataset_versions.id"
_RUN_FK = "ml_training_runs.id"
_MODEL_FK = "ml_model_versions.id"
# Python None is stored as SQL NULL (not the JSON value null), so IS NULL checks hold.
NULLABLE_JSON = JSONB(none_as_null=True)
# Analytical values (labels, scores). NUMERIC, never a float column: written from exact
# decimal representations and read back as Python floats.
REAL_NUMBER = Numeric(asdecimal=False)


class MLDatasetRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_datasets"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint(f"name ~ '{NAME}'", name="name_format"),
    )

    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLDatasetVersionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version"),
        Index(None, "content_sha256"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("matured_count <= observation_count", name="matured_le_total"),
        CheckConstraint(
            "decision_start IS NULL OR decision_end IS NULL OR decision_start <= decision_end",
            name="decision_range",
        ),
        CheckConstraint(
            "decision_end IS NULL OR decision_end <= data_known_at", name="decisions_known"
        ),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ml_datasets.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    # The validated specification it was built from, and its hash: rebuilding the same
    # specification against the same stored data reproduces content_sha256.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spec_sha256: Mapped[str] = mapped_column(String(64))
    # Ordered feature keys with their registry definitions (name, version, sha256).
    feature_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    feature_schema_sha256: Mapped[str] = mapped_column(String(64))
    feature_registry_version: Mapped[str] = mapped_column(String(32))
    target_kind: Mapped[TargetKind] = mapped_column(str_enum(TargetKind, "ml_target_kind"))
    target_definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    target_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[ObservationSource] = mapped_column(
        str_enum(ObservationSource, "ml_observation_source")
    )
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    origin: Mapped[DataOrigin] = mapped_column(str_enum(DataOrigin, "ml_data_origin"))
    # Bars and stored features are read as Atlas knew them at this instant; labels are
    # matured only if their outcome period had settled by then.
    data_known_at: Mapped[datetime]
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    adjustment: Mapped[str] = mapped_column(String(16))
    timeframes: Mapped[list[str]] = mapped_column(JSONB)
    asset_count: Mapped[int] = mapped_column(Integer)
    observation_count: Mapped[int] = mapped_column(Integer)
    matured_count: Mapped[int] = mapped_column(Integer)
    decision_start: Mapped[datetime | None]
    decision_end: Mapped[datetime | None]
    # Counts by label status, direction, rejections, label balance, overlap.
    statistics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(JSONB)
    # sha256 over the ordered observations (identity, features and labels).
    content_sha256: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLDatasetObservationRecord(Base):
    __tablename__ = "ml_dataset_observations"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "row_index"),
        Index(None, "dataset_version_id", "decision_at"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("row_index >= 0", name="row_index_nonnegative"),
        CheckConstraint("horizon_periods >= 1", name="horizon_positive"),
        CheckConstraint(
            "features_available_at IS NULL OR features_available_at <= data_known_at",
            name="features_known",
        ),
        CheckConstraint(
            "last_input_period_at IS NULL OR last_input_period_at <= decision_at",
            name="no_future_inputs",
        ),
        CheckConstraint(
            "label_status <> 'MATURED' OR (label_end_at IS NOT NULL AND label_end_at > "
            "decision_at AND label_end_at <= data_known_at)",
            name="matured_label_window",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_DATASET_VERSION_FK, ondelete="RESTRICT")
    )
    row_index: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction, "proposal_direction"))
    # The decision time (data cutoff) and when the dataset read the data.
    decision_at: Mapped[datetime]
    data_known_at: Mapped[datetime]
    source: Mapped[ObservationSource] = mapped_column(
        str_enum(ObservationSource, "ml_observation_source")
    )
    # Strategy observations: the strategy, its logic version and parameter hash, and the
    # stored proposal when there is one.
    strategy_id: Mapped[str | None] = mapped_column(String(48))
    strategy_version: Mapped[str | None] = mapped_column(String(160))
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    horizon_periods: Mapped[int] = mapped_column(Integer)
    # Feature key -> {"value", "status", "available_at"} (point in time).
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    features_available_at: Mapped[datetime | None]
    last_input_period_at: Mapped[datetime | None]
    # Segments used to evaluate models (descriptive; see ml_engine.datasets.segments).
    segments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Label: status, value, class and the realized outcome behind it.
    label_status: Mapped[LabelStatus] = mapped_column(str_enum(LabelStatus, "ml_label_status"))
    label_value: Mapped[float | None] = mapped_column(REAL_NUMBER)
    label_class: Mapped[int | None] = mapped_column(Integer)
    label_end_at: Mapped[datetime | None]
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB)


class MLTrainingRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_training_runs"
    __table_args__ = (
        Index(None, "dataset_version_id", "started_at"),
        Index(None, "status", "started_at"),
        CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name="finished_iff_done"),
        CheckConstraint("(status = 'FAILED') = (error IS NOT NULL)", name="error_iff_failed"),
    )

    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_DATASET_VERSION_FK, ondelete="RESTRICT")
    )
    model_type: Mapped[ModelType] = mapped_column(str_enum(ModelType, "ml_model_type"))
    task: Mapped[TaskKind] = mapped_column(str_enum(TaskKind, "ml_task_kind"))
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "ml_run_status"))
    # Hyperparameters (and the grid, if any), splits, preprocessing and seed as requested.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spec_sha256: Mapped[str] = mapped_column(String(64))
    random_seed: Mapped[int] = mapped_column(Integer)
    target_definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    code_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Filled when the run finishes.
    periods: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    feature_schema: Mapped[list[dict[str, Any]] | None] = mapped_column(NULLABLE_JSON)
    preprocessing: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    training_metrics: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    validation_metrics: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    test_metrics: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    baseline_metrics: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    walk_forward: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    robustness: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    selection: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    validation_status: Mapped[ValidationStatus | None] = mapped_column(
        str_enum(ValidationStatus, "ml_validation_status")
    )
    validation_reasons: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    # Earlier runs on the same dataset version whose test period was the same.
    test_period_reuse: Mapped[int] = mapped_column(Integer, server_default="0")
    notes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class MLModelArtifactRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_model_artifacts"
    __table_args__ = (
        UniqueConstraint("training_run_id"),
        UniqueConstraint("path"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    training_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    path: Mapped[str] = mapped_column(String(256))  # relative to ML_ARTIFACT_DIR
    format: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    libraries: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLModelVersionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_model_versions"
    __table_args__ = (
        UniqueConstraint("model_name", "version"),
        UniqueConstraint("training_run_id"),
        CheckConstraint(f"model_name ~ '{NAME}'", name="model_name_format"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "output_kind <> 'CALIBRATED_PROBABILITY' OR calibration_method <> 'NONE'",
            name="probability_needs_calibration",
        ),
    )

    model_name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    model_type: Mapped[ModelType] = mapped_column(str_enum(ModelType, "ml_model_type"))
    task: Mapped[TaskKind] = mapped_column(str_enum(TaskKind, "ml_task_kind"))
    training_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_DATASET_VERSION_FK, ondelete="RESTRICT")
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ml_model_artifacts.id", ondelete="RESTRICT")
    )
    # The inputs inference must receive (keys, kinds, whether missing is allowed).
    input_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    input_schema_sha256: Mapped[str] = mapped_column(String(64))
    target_definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evaluation_summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    output_kind: Mapped[OutputKind] = mapped_column(str_enum(OutputKind, "ml_output_kind"))
    calibration_method: Mapped[CalibrationMethod] = mapped_column(
        str_enum(CalibrationMethod, "ml_calibration_method")
    )
    validation_status: Mapped[ValidationStatus] = mapped_column(
        str_enum(ValidationStatus, "ml_validation_status")
    )
    origin: Mapped[DataOrigin] = mapped_column(str_enum(DataOrigin, "ml_data_origin"))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLModelStatusEventRecord(Base):
    __tablename__ = "ml_model_status_events"
    __table_args__ = (
        Index(None, "model_version_id", "id"),
        Index(None, "model_name", "id"),
        # One successor per event: concurrent transitions from the same state cannot both
        # commit (the first event has no predecessor).
        UniqueConstraint(
            "model_version_id",
            "previous_event_id",
            postgresql_nulls_not_distinct=True,
            name="uq_ml_model_status_events_transition",
        ),
        CheckConstraint(
            "(previous_event_id IS NULL) = (from_status IS NULL)", name="first_has_no_origin"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    model_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_MODEL_FK, ondelete="RESTRICT"))
    model_name: Mapped[str] = mapped_column(String(64))
    previous_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ml_model_status_events.id", ondelete="RESTRICT")
    )
    from_status: Mapped[LifecycleStatus | None] = mapped_column(
        str_enum(LifecycleStatus, "ml_lifecycle_from_status")
    )
    to_status: Mapped[LifecycleStatus] = mapped_column(
        str_enum(LifecycleStatus, "ml_lifecycle_status")
    )
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLModelEvaluationRecord(Base):
    __tablename__ = "ml_model_evaluations"
    __table_args__ = (Index(None, "training_run_id", "split", "segment_kind"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    training_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    split: Mapped[SplitKind] = mapped_column(str_enum(SplitKind, "ml_split_kind"))
    fold: Mapped[int | None] = mapped_column(Integer)
    segment_kind: Mapped[SegmentKind] = mapped_column(str_enum(SegmentKind, "ml_segment_kind"))
    segment_value: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[datetime | None]
    period_end: Mapped[datetime | None]
    observations: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    baseline_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLCalibrationResultRecord(Base):
    __tablename__ = "ml_calibration_results"
    __table_args__ = (Index(None, "training_run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    training_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    method: Mapped[CalibrationMethod] = mapped_column(
        str_enum(CalibrationMethod, "ml_calibration_method")
    )
    # Where it was fitted (always validation data) and where the evidence was measured.
    fitted_on: Mapped[SplitKind] = mapped_column(str_enum(SplitKind, "ml_split_kind"))
    evaluated_on: Mapped[str] = mapped_column(String(32))
    observations: Mapped[int] = mapped_column(Integer)
    brier_raw: Mapped[float | None] = mapped_column(REAL_NUMBER)
    brier_calibrated: Mapped[float | None] = mapped_column(REAL_NUMBER)
    brier_base_rate: Mapped[float | None] = mapped_column(REAL_NUMBER)
    ece_raw: Mapped[float | None] = mapped_column(REAL_NUMBER)
    ece_calibrated: Mapped[float | None] = mapped_column(REAL_NUMBER)
    reliability_raw: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reliability_calibrated: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    selected: Mapped[bool] = mapped_column(Boolean)
    accepted: Mapped[bool] = mapped_column(Boolean)
    decision: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MLPredictionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ml_predictions"
    __table_args__ = (
        Index(None, "model_version_id", "predicted_at"),
        Index(None, "asset_id", "predicted_at"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("(validity = 'VALID') = (output IS NOT NULL)", name="output_iff_valid"),
        CheckConstraint(
            "inputs_available_at IS NULL OR inputs_available_at <= predicted_at",
            name="inputs_before_prediction",
        ),
    )

    model_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_MODEL_FK, ondelete="RESTRICT"))
    model_status: Mapped[LifecycleStatus] = mapped_column(
        str_enum(LifecycleStatus, "ml_lifecycle_status")
    )
    mode: Mapped[PredictionMode] = mapped_column(str_enum(PredictionMode, "ml_prediction_mode"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    timeframe: Mapped[str] = mapped_column(String(16))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction, "proposal_direction"))
    strategy_id: Mapped[str | None] = mapped_column(String(48))
    decision_at: Mapped[datetime]
    predicted_at: Mapped[datetime]
    # The exact inputs received (feature key -> value/status/available_at) and their hash.
    feature_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    feature_snapshot_sha256: Mapped[str] = mapped_column(String(64))
    inputs_available_at: Mapped[datetime | None]
    input_schema_sha256: Mapped[str] = mapped_column(String(64))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    validity: Mapped[PredictionValidity] = mapped_column(
        str_enum(PredictionValidity, "ml_prediction_validity")
    )
    problems: Mapped[list[str]] = mapped_column(JSONB)
    output_kind: Mapped[OutputKind] = mapped_column(str_enum(OutputKind, "ml_output_kind"))
    output: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    requested_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "MLCalibrationResultRecord",
    "MLDatasetObservationRecord",
    "MLDatasetRecord",
    "MLDatasetVersionRecord",
    "MLModelArtifactRecord",
    "MLModelEvaluationRecord",
    "MLModelStatusEventRecord",
    "MLModelVersionRecord",
    "MLPredictionRecord",
    "MLTrainingRunRecord",
]

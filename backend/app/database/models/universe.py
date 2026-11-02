"""Discovery jobs, qualification, watchlist scoring and universe membership (Phase 3A).

* ``discovery_jobs``: every discovery, refresh, qualification, scoring and membership job,
  with leases for crash recovery. A partial unique index allows one active (PENDING or
  RUNNING) job per kind and provider, so repeated requests cannot overlap.
* ``discovery_results``: per-instrument outcomes that need attention (new, changed,
  rejected, conflicting, missing). Unchanged instruments are only counted.
* ``qualification_rule_sets`` / ``scoring_versions``: immutable, versioned configuration.
  A change is a new version; evaluations and scores reference the version they used.
* ``asset_metric_values``: reusable measurements (volume, dollar volume, spread, ATR,
  volatility, completeness) with their window, as-of time, source range and version.
* ``qualification_evaluations`` / ``watchlist_scores``: the evidence behind membership.
* ``universe_membership_events``: immutable ADDED/REMOVED events per asset and layer. Each
  event names the event it follows (``previous_event_id``); a unique constraint on it makes
  two concurrent transitions from the same state impossible. Membership at time T is the
  latest event with ``effective_at <= T`` that was recorded by the time it is asked about.

All of these tables except ``discovery_jobs`` and the configuration's activation are
append-only (database triggers, migration 0006).
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    DiscoveryOutcome,
    JobTrigger,
    MembershipAction,
    MetricStatus,
    QualificationOutcome,
    UniverseLayer,
)
from app.database.models.base import Base, str_enum


class DiscoveryJobRecord(Base):
    __tablename__ = "discovery_jobs"
    __table_args__ = (
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED') "
            "OR completed_at IS NOT NULL",
            name="finished_has_completion_time",
        ),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint(
            "status <> 'RUNNING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        Index(
            "uq_discovery_jobs_one_active_per_kind",
            "kind",
            "provider",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        Index(None, "status", "next_attempt_at"),
        Index(None, "kind", "created_at"),
        Index(None, "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    kind: Mapped[DiscoveryJobKind] = mapped_column(str_enum(DiscoveryJobKind, "discovery_job_kind"))
    provider: Mapped[str] = mapped_column(
        String(32), ForeignKey("market_data_providers.code", ondelete="RESTRICT")
    )
    status: Mapped[DiscoveryJobStatus] = mapped_column(
        str_enum(DiscoveryJobStatus, "discovery_job_status")
    )
    trigger: Mapped[JobTrigger] = mapped_column(str_enum(JobTrigger, "job_trigger"))
    requested_by: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # A pipeline run chains jobs (discovery -> qualification -> scoring -> membership).
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    attempt: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    lease_expires_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    summary: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    recent_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class DiscoveryResultRecord(Base):
    __tablename__ = "discovery_results"
    __table_args__ = (Index(None, "job_id", "outcome"), Index(None, "asset_id"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="RESTRICT"))
    provider_asset_id: Mapped[str | None] = mapped_column(String(64))
    provider_symbol: Mapped[str | None] = mapped_column(String(128))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    outcome: Mapped[DiscoveryOutcome] = mapped_column(
        str_enum(DiscoveryOutcome, "discovery_outcome")
    )
    detail: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class _VersionedConfig:
    """Immutable named configuration; a change is a new ``version`` of the same ``code``."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]]
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class QualificationRuleSetRecord(_VersionedConfig, Base):
    __tablename__ = "qualification_rule_sets"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        UniqueConstraint("code", "content_sha256"),
        CheckConstraint("version >= 1", name="version_positive"),
    )


class ScoringVersionRecord(_VersionedConfig, Base):
    __tablename__ = "scoring_versions"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        UniqueConstraint("code", "content_sha256"),
        CheckConstraint("version >= 1", name="version_positive"),
    )


class AssetMetricValueRecord(Base):
    __tablename__ = "asset_metric_values"
    __table_args__ = (
        # One value per job, asset and measurement (two jobs may share an as-of time).
        UniqueConstraint(
            "job_id",
            "asset_id",
            "metric",
            "window",
            "feature_version",
            name="uq_asset_metric_values_measurement",
        ),
        CheckConstraint(
            "source_start IS NULL OR source_end IS NULL OR source_start <= source_end",
            name="source_range_ordered",
        ),
        CheckConstraint("source_end IS NULL OR source_end <= as_of", name="no_future_source"),
        CheckConstraint("(status = 'OK') = (value IS NOT NULL)", name="value_iff_ok"),
        Index(None, "asset_id", "as_of"),
        Index(None, "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    metric: Mapped[str] = mapped_column(String(64))
    # e.g. "20 sessions": the lookback the value was computed over.
    window: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    feature_version: Mapped[str] = mapped_column(String(32))
    # Only data available to Atlas at ``as_of`` is used.
    as_of: Mapped[datetime]
    source_start: Mapped[datetime | None]
    source_end: Mapped[datetime | None]
    observations: Mapped[int] = mapped_column(Integer)
    expected_observations: Mapped[int] = mapped_column(Integer)
    value: Mapped[Decimal | None]
    status: Mapped[MetricStatus] = mapped_column(str_enum(MetricStatus, "metric_status"))
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class QualificationEvaluationRecord(Base):
    __tablename__ = "qualification_evaluations"
    __table_args__ = (
        UniqueConstraint("job_id", "asset_id"),
        Index(None, "asset_id", "as_of"),
        Index(None, "as_of"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qualification_rule_sets.id", ondelete="RESTRICT")
    )
    as_of: Mapped[datetime]
    outcome: Mapped[QualificationOutcome] = mapped_column(
        str_enum(QualificationOutcome, "qualification_outcome")
    )
    # [{rule, category, mandatory, status, value, threshold, message}, ...]
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    failed_rules: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    unavailable_rules: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    # Reference-data short/borrow flags as known at ``as_of``. Informational only: borrow
    # must be revalidated when a short is actually proposed.
    short_eligibility: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    reference_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("asset_reference_versions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WatchlistScoreRecord(Base):
    __tablename__ = "watchlist_scores"
    __table_args__ = (
        UniqueConstraint("job_id", "asset_id"),
        UniqueConstraint("job_id", "rank"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("overall_score >= 0 AND overall_score <= 100", name="score_range"),
        # Unranked: qualified, but too few factors computable to rank.
        CheckConstraint("(rank IS NULL) = (overall_score IS NULL)", name="ranked_iff_scored"),
        CheckConstraint("rank IS NOT NULL OR NOT selected", name="selected_is_ranked"),
        Index(None, "asset_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    scoring_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scoring_versions.id", ondelete="RESTRICT")
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qualification_evaluations.id", ondelete="RESTRICT")
    )
    as_of: Mapped[datetime]
    overall_score: Mapped[Decimal | None]
    rank: Mapped[int | None] = mapped_column(Integer)
    # Share of the scoring weight whose factors had data.
    weight_coverage: Mapped[Decimal]
    # {factor: {raw, score, weight, available, explanation}}
    factor_scores: Mapped[dict[str, Any]]
    explanation: Mapped[str] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UniverseMembershipEventRecord(Base):
    __tablename__ = "universe_membership_events"
    __table_args__ = (
        # At most one successor per event (and one first event per asset/layer): concurrent
        # writers deciding from the same state cannot both commit.
        UniqueConstraint(
            "asset_id",
            "layer",
            "previous_event_id",
            postgresql_nulls_not_distinct=True,
            name="uq_universe_membership_events_transition",
        ),
        CheckConstraint("effective_at <= recorded_at", name="effective_not_after_recorded"),
        CheckConstraint(
            "(previous_event_id IS NULL) = (action = 'ADDED' AND sequence = 1)",
            name="first_event_is_addition",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        Index(None, "layer", "effective_at"),
        Index(None, "asset_id", "layer", "sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    layer: Mapped[UniverseLayer] = mapped_column(str_enum(UniverseLayer, "universe_layer"))
    action: Mapped[MembershipAction] = mapped_column(
        str_enum(MembershipAction, "membership_action")
    )
    # Position in this asset's history for the layer (1 = first event, always ADDED).
    sequence: Mapped[int] = mapped_column(Integer)
    previous_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("universe_membership_events.id", ondelete="RESTRICT")
    )
    # When the membership changed (the evaluation's as-of time).
    effective_at: Mapped[datetime]
    # When Atlas recorded it; point-in-time queries ignore events recorded later.
    recorded_at: Mapped[datetime]
    reason_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification_evaluations.id", ondelete="RESTRICT")
    )
    score_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("watchlist_scores.id", ondelete="RESTRICT")
    )
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")

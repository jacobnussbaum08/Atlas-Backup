"""Market intelligence storage (Phase 3B).

* ``intelligence_configs``: immutable, versioned algorithm configuration (as 3A rule sets).
* ``intelligence_runs``: one computation (LIVE: now; RECONSTRUCTION: a range of past
  decision times). The only mutable table here; a partial unique index allows one RUNNING
  run per provider and feed.
* ``market_feature_snapshots``: a feature set per instrument, timeframe and decision time.
* ``regime_classifications``: trend, volatility and stability classifications with their
  probabilities and evidence, for an instrument (ASSET) or the broad market (MARKET).
* ``market_summaries``: universe participation and market-wide figures per decision time.
* ``cross_asset_relationships``: pairwise and benchmark correlations per decision time.

Every result row records its decision time (``as_of``), the knowledge basis of its inputs,
when its newest input became available (``inputs_available_at``), when the row itself
became available to Atlas (``available_at``), the algorithm and configuration version and
a quality status. Result tables are append-only (triggers, migration 0007): a later
correction of a bar creates new results in a new run and never rewrites earlier ones.
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

from app.data_engine.enums import PriceAdjustment
from app.database.models.base import Base, str_enum
from app.database.models.universe import VersionedConfig
from app.feature_engine.features import FeatureQuality
from app.market_intelligence.enums import (
    ClassificationQuality,
    Dimension,
    KnowledgeBasis,
    RelationKind,
    RunKind,
    RunStatus,
    Scope,
)

TIMEFRAME = "^(1Day|1Week)$"
PROBABILITY = Numeric(7, 6)
STATISTIC = Numeric(12, 8)


class IntelligenceConfigRecord(VersionedConfig, Base):
    __tablename__ = "intelligence_configs"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        UniqueConstraint("code", "content_sha256"),
        CheckConstraint("version >= 1", name="version_positive"),
    )


class IntelligenceRunRecord(Base):
    __tablename__ = "intelligence_runs"
    __table_args__ = (
        Index(
            "uq_intelligence_runs_running",
            "provider",
            "feed",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index(None, "created_at"),
        CheckConstraint("as_of_end >= as_of_start", name="as_of_ordered"),
        CheckConstraint(
            "(status = 'RUNNING') = (completed_at IS NULL)", name="completed_iff_finished"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    kind: Mapped[RunKind] = mapped_column(str_enum(RunKind, "intelligence_run_kind"))
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "intelligence_run_status"))
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    adjustment: Mapped[PriceAdjustment] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    benchmark_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intelligence_configs.id", ondelete="RESTRICT")
    )
    as_of_start: Mapped[datetime]
    as_of_end: Mapped[datetime]
    requested_by: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    summary: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime]
    # Renewed while the run works; a RUNNING run with a stale heartbeat is an orphan.
    heartbeat_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class _Result:
    """Columns every result row carries."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intelligence_runs.id", ondelete="RESTRICT")
    )
    # Decision time: only inputs available by then (under ``basis``) were used.
    as_of: Mapped[datetime]
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    # When this row became available to Atlas (its computation time).
    available_at: Mapped[datetime]


class MarketFeatureSnapshotRecord(_Result, Base):
    __tablename__ = "market_feature_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "asset_id", "timeframe", "as_of"),
        Index(None, "asset_id", "timeframe", "as_of"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("available_at >= as_of", name="available_after_decision"),
        CheckConstraint(
            "inputs_available_at IS NULL OR basis <> 'RECORDED' OR inputs_available_at <= as_of",
            name="recorded_inputs_known",
        ),
        CheckConstraint("last_event_at IS NULL OR last_event_at <= as_of", name="no_future_bar"),
        CheckConstraint(
            "(quality = 'INSUFFICIENT_DATA') = (feature_values = '{}'::jsonb)",
            name="values_iff_sufficient",
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    timeframe: Mapped[str] = mapped_column(String(16))
    feature_set_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    adjustment: Mapped[PriceAdjustment] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    quality: Mapped[FeatureQuality] = mapped_column(str_enum(FeatureQuality, "feature_quality"))
    # Feature name -> decimal string (or null when undefined).
    feature_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    window_bars: Mapped[int] = mapped_column(Integer)
    bars_used: Mapped[int] = mapped_column(Integer)
    expected_bars: Mapped[int] = mapped_column(Integer)
    missing_bars: Mapped[int] = mapped_column(Integer)
    first_event_at: Mapped[datetime | None]
    last_event_at: Mapped[datetime | None]
    inputs_available_at: Mapped[datetime | None]
    notes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")


class RegimeClassificationRecord(_Result, Base):
    __tablename__ = "regime_classifications"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "scope",
            "asset_id",
            "timeframe",
            "dimension",
            "as_of",
            postgresql_nulls_not_distinct=True,
        ),
        Index(None, "scope", "dimension", "timeframe", "as_of"),
        Index(None, "asset_id", "as_of"),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME}'", name="timeframe_format"),
        CheckConstraint("(scope = 'MARKET') = (asset_id IS NULL)", name="scope_matches_asset"),
        CheckConstraint("available_at >= as_of", name="available_after_decision"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    scope: Mapped[Scope] = mapped_column(str_enum(Scope, "intelligence_scope"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    timeframe: Mapped[str] = mapped_column(String(16))
    dimension: Mapped[Dimension] = mapped_column(str_enum(Dimension, "intelligence_dimension"))
    algorithm: Mapped[str] = mapped_column(String(48))
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intelligence_configs.id", ondelete="RESTRICT")
    )
    label: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[Decimal] = mapped_column(PROBABILITY)
    # Label -> probability (decimal string); sums to 1 including UNCERTAIN.
    probabilities: Mapped[dict[str, Any]] = mapped_column(JSONB)
    quality: Mapped[ClassificationQuality] = mapped_column(
        str_enum(ClassificationQuality, "classification_quality")
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    reasons: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    inputs_available_at: Mapped[datetime | None]


class MarketSummaryRecord(_Result, Base):
    __tablename__ = "market_summaries"
    __table_args__ = (
        UniqueConstraint("run_id", "as_of"),
        Index(None, "as_of"),
        CheckConstraint("available_at >= as_of", name="available_after_decision"),
        CheckConstraint(
            "members_evaluated >= 0 AND members_evaluated <= members_total",
            name="members_counted",
        ),
    )

    benchmark_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    algorithm: Mapped[str] = mapped_column(String(48))
    # Participation status: OK or INSUFFICIENT_DATA.
    participation_status: Mapped[str] = mapped_column(String(24))
    members_total: Mapped[int] = mapped_column(Integer)
    members_evaluated: Mapped[int] = mapped_column(Integer)
    participation: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # Cross-asset aggregates: average correlation, common exposure (absorption ratio).
    cross_asset: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    limitations: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")


class CrossAssetRelationshipRecord(_Result, Base):
    __tablename__ = "cross_asset_relationships"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "asset_a", "asset_b", "as_of"),
        Index(None, "asset_a", "as_of"),
        CheckConstraint("asset_a <> asset_b", name="distinct_assets"),
        CheckConstraint("available_at >= as_of", name="available_after_decision"),
        CheckConstraint("(status = 'OK') = (correlation IS NOT NULL)", name="correlation_iff_ok"),
        CheckConstraint(
            "correlation IS NULL OR (correlation >= -1 AND correlation <= 1)",
            name="correlation_range",
        ),
    )

    kind: Mapped[RelationKind] = mapped_column(str_enum(RelationKind, "relation_kind"))
    asset_a: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    # For BENCHMARK rows: the benchmark.
    asset_b: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    algorithm: Mapped[str] = mapped_column(String(48))
    window: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime | None]
    window_end: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(24))
    observations: Mapped[int] = mapped_column(Integer)
    correlation: Mapped[Decimal | None] = mapped_column(STATISTIC)
    ci_low: Mapped[Decimal | None] = mapped_column(STATISTIC)
    ci_high: Mapped[Decimal | None] = mapped_column(STATISTIC)
    significant: Mapped[bool | None]
    prior_observations: Mapped[int] = mapped_column(Integer, server_default="0")
    prior_correlation: Mapped[Decimal | None] = mapped_column(STATISTIC)
    change: Mapped[Decimal | None] = mapped_column(STATISTIC)
    beta: Mapped[Decimal | None] = mapped_column(STATISTIC)
    relative_log_return: Mapped[Decimal | None] = mapped_column(STATISTIC)


__all__ = [
    "CrossAssetRelationshipRecord",
    "IntelligenceConfigRecord",
    "IntelligenceRunRecord",
    "MarketFeatureSnapshotRecord",
    "MarketSummaryRecord",
    "RegimeClassificationRecord",
]

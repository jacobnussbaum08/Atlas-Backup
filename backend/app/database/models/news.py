"""News and event intelligence storage (Phase 3C).

* ``news_providers``: registered news and economic-calendar providers, with the symbol
  namespace their article symbols use and their content policy.
* ``news_sources``: publishers/feeds as the provider names them (e.g. ``benzinga``).
  Reliability is ``NOT_ASSESSED``: Atlas has no basis for rating publishers yet.
* ``news_articles``: one row per provider article: identity plus the *current* state,
  materialised from its newest revision (as ``assets`` from reference versions, D-052).
* ``news_article_revisions``: every distinct version Atlas observed, append-only, with the
  provider's timestamps and Atlas's own (received, processed, available) kept apart.
* ``news_asset_mappings``: the outcome of mapping each symbol of a revision to an Atlas
  asset, effective-dated at publication (append-only).
* ``news_ingestion_jobs``: historical retrievals (operator backfills, stream recovery).
* ``news_stream_sessions`` / ``news_stream_status``: the live news worker's connections
  and current state, separate from the market-data stream's.
* ``news_events`` / ``event_classifications`` / ``event_asset_relationships``: what a
  revision is about under a versioned classifier, the categories with method, evidence
  and confidence, and the assets it concerns with explained relevance (append-only).
* ``economic_events`` / ``economic_event_revisions``: scheduled economic releases. Empty:
  no calendar provider is connected (D-077).

Availability (D-075): ``received_at`` is when Atlas obtained an observation; ``available_at``
when the stored revision became usable (>= ``received_at``); ``original_available_at`` is set
only for observations received live over the stream, and is NULL for every REST retrieval:
a backfill never claims Atlas knew the article at its publication time. ``published_at``
and ``provider_updated_at`` are the provider's own timestamps.
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
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, str_enum
from app.news.enums import (
    ArticleOrigin,
    AssetRelationship,
    ClassificationConfidence,
    ClassificationMethod,
    ClassificationSource,
    EconomicImportance,
    EventCategory,
    EventScope,
    MappingStatus,
    NewsJobKind,
    NewsJobStatus,
    ProviderKind,
    RevisionReason,
    SourceReliability,
    VerificationStatus,
)

_PROVIDER_FK = "news_providers.code"
_JOB_COUNTERS = (
    "pages_retrieved >= 0 AND articles_received >= 0 AND articles_new >= 0 AND "
    "revisions_new >= 0 AND duplicates >= 0 AND malformed >= 0"
)


class NewsProviderRecord(Base):
    __tablename__ = "news_providers"
    __table_args__ = (CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,31}$'", name="code_format"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[ProviderKind] = mapped_column(str_enum(ProviderKind, "news_provider_kind"))
    # The market-data provider whose symbols the articles carry (asset mapping).
    symbol_namespace: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("market_data_providers.code", ondelete="RESTRICT")
    )
    content_policy: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NewsSourceRecord(Base):
    __tablename__ = "news_sources"
    __table_args__ = (UniqueConstraint("provider", "code"),)

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    reliability: Mapped[SourceReliability] = mapped_column(
        str_enum(SourceReliability, "news_source_reliability"), server_default="NOT_ASSESSED"
    )
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]


class NewsIngestionJobRecord(Base):
    __tablename__ = "news_ingestion_jobs"
    __table_args__ = (
        CheckConstraint("range_end > range_start", name="range_ordered"),
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') OR completed_at IS NOT NULL",
            name="finished_has_completion_time",
        ),
        CheckConstraint(_JOB_COUNTERS, name="counters_non_negative"),
        Index(None, "status", "created_at"),
        Index(None, "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"))
    kind: Mapped[NewsJobKind] = mapped_column(str_enum(NewsJobKind, "news_job_kind"))
    status: Mapped[NewsJobStatus] = mapped_column(
        str_enum(NewsJobStatus, "news_job_status"), server_default="PENDING"
    )
    # Provider symbols requested; empty means all news.
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    range_start: Mapped[datetime]
    range_end: Mapped[datetime]
    include_content: Mapped[bool] = mapped_column(Boolean, server_default="false")
    requested_by: Mapped[str] = mapped_column(String(64))
    # Recovery jobs name the stream session whose (re)connection they cover.
    stream_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_stream_sessions.id", ondelete="RESTRICT")
    )
    params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    pages_retrieved: Mapped[int] = mapped_column(Integer, server_default="0")
    articles_received: Mapped[int] = mapped_column(Integer, server_default="0")
    articles_new: Mapped[int] = mapped_column(Integer, server_default="0")
    revisions_new: Mapped[int] = mapped_column(Integer, server_default="0")
    duplicates: Mapped[int] = mapped_column(Integer, server_default="0")
    malformed: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class NewsStreamSessionRecord(Base):
    """One connection attempt of the news stream worker, from connect to close."""

    __tablename__ = "news_stream_sessions"
    __table_args__ = (Index(None, "provider", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"))
    worker_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(256))
    requested_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    confirmed_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    started_at: Mapped[datetime]
    authenticated_at: Mapped[datetime | None]
    subscribed_at: Mapped[datetime | None]
    first_message_at: Mapped[datetime | None]
    last_message_at: Mapped[datetime | None]
    last_article_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(Text)
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")


class NewsStreamStatusRecord(Base):
    """Current state of the news stream worker for one provider (heartbeat-updated)."""

    __tablename__ = "news_stream_status"

    provider: Mapped[str] = mapped_column(
        String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"), primary_key=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool]
    connection_state: Mapped[str] = mapped_column(String(32))
    auth_state: Mapped[str] = mapped_column(String(32))
    subscription_state: Mapped[str] = mapped_column(String(32))
    current_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_stream_sessions.id", ondelete="RESTRICT")
    )
    requested_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    confirmed_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    worker_started_at: Mapped[datetime | None]
    worker_stopped_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime]
    last_authenticated_at: Mapped[datetime | None]
    last_subscribed_at: Mapped[datetime | None]
    last_message_at: Mapped[datetime | None]
    last_article_at: Mapped[datetime | None]
    last_persisted_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    recent_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    reconnect_count: Mapped[int] = mapped_column(Integer, server_default="0")
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class NewsArticleRecord(Base):
    """Article identity and current state. History is in ``news_article_revisions``."""

    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("provider", "provider_article_id"),
        CheckConstraint("current_revision >= 1", name="revision_positive"),
        CheckConstraint("first_available_at >= first_received_at", name="available_after_received"),
        Index(None, "published_at"),
        Index(None, "source_id", "published_at"),
        Index(None, "primary_category", "published_at"),
        Index("ix_news_articles_mapped_asset_ids", "mapped_asset_ids", postgresql_using="gin"),
        Index("ix_news_articles_symbols", "symbols", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"))
    provider_article_id: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT")
    )
    # Provider publication time (``created_at`` at Alpaca), as currently reported.
    published_at: Mapped[datetime]
    provider_updated_at: Mapped[datetime | None]
    # Atlas's first contact with the article, by any route, and how it came.
    first_received_at: Mapped[datetime]
    first_available_at: Mapped[datetime]
    first_origin: Mapped[ArticleOrigin] = mapped_column(str_enum(ArticleOrigin, "news_origin"))
    # First receipt over the live stream, if any: the only evidence of real-time availability.
    first_live_received_at: Mapped[datetime | None]
    current_revision: Mapped[int] = mapped_column(Integer)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(2048))
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapped_asset_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()), server_default="{}")
    primary_category: Mapped[EventCategory | None] = mapped_column(
        str_enum(EventCategory, "event_category")
    )
    scope: Mapped[EventScope | None] = mapped_column(str_enum(EventScope, "event_scope"))
    classifier_version: Mapped[str | None] = mapped_column(String(48))
    observation_count: Mapped[int] = mapped_column(Integer, server_default="1")
    last_seen_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class NewsArticleRevisionRecord(Base):
    __tablename__ = "news_article_revisions"
    __table_args__ = (
        UniqueConstraint("article_id", "revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("available_at >= received_at", name="available_after_received"),
        CheckConstraint("processed_at >= received_at", name="processed_after_received"),
        # A REST retrieval never claims a real-time availability time (D-075).
        CheckConstraint(
            "(origin = 'REALTIME_STREAM') = (original_available_at IS NOT NULL)",
            name="original_availability_only_live",
        ),
        CheckConstraint(
            "original_available_at IS NULL OR original_available_at <= available_at",
            name="original_before_available",
        ),
        CheckConstraint(
            "origin <> 'HISTORICAL_REST' OR ingestion_job_id IS NOT NULL", name="rest_has_job"
        ),
        CheckConstraint(
            "origin <> 'REALTIME_STREAM' OR stream_session_id IS NOT NULL",
            name="stream_has_session",
        ),
        CheckConstraint("content IS NULL OR has_body", name="content_only_with_body"),
        Index(None, "available_at"),
        Index(None, "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_articles.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    reason: Mapped[RevisionReason] = mapped_column(str_enum(RevisionReason, "news_revision_reason"))
    origin: Mapped[ArticleOrigin] = mapped_column(str_enum(ArticleOrigin, "news_origin"))
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(256))
    source_code: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(2048))
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    published_at: Mapped[datetime]
    provider_updated_at: Mapped[datetime | None]
    # The body only when NEWS_STORE_CONTENT permits it (D-074); its hash either way.
    content: Mapped[str | None] = mapped_column(Text)
    has_body: Mapped[bool] = mapped_column(Boolean)
    body_sha256: Mapped[str | None] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    provider_categories: Mapped[list[str]] = mapped_column(ARRAY(String(128)), server_default="{}")
    provider_payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime]
    available_at: Mapped[datetime]
    original_available_at: Mapped[datetime | None]
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_ingestion_jobs.id", ondelete="RESTRICT")
    )
    stream_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_stream_sessions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NewsAssetMappingRecord(Base):
    __tablename__ = "news_asset_mappings"
    __table_args__ = (
        UniqueConstraint("revision_id", "provider_symbol", "mapping_version"),
        CheckConstraint("(status = 'MAPPED') = (asset_id IS NOT NULL)", name="asset_iff_mapped"),
        Index(None, "asset_id"),
        Index(None, "article_id"),
        Index(None, "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("news_article_revisions.id", ondelete="RESTRICT")
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_articles.id", ondelete="RESTRICT")
    )
    provider_symbol: Mapped[str] = mapped_column(String(32))
    symbol_namespace: Mapped[str] = mapped_column(String(32))
    status: Mapped[MappingStatus] = mapped_column(str_enum(MappingStatus, "news_mapping_status"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    asset_identifier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_identifiers.id", ondelete="RESTRICT")
    )
    # The asset's status when the mapping was made (an inactive asset is still mapped).
    asset_status: Mapped[str | None] = mapped_column(String(16))
    # The time the symbol was resolved for: the article's publication time.
    resolved_for: Mapped[datetime]
    mapping_version: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NewsEventRecord(Base):
    """What one article revision is about, under one classifier version."""

    __tablename__ = "news_events"
    __table_args__ = (
        UniqueConstraint("revision_id", "classifier_version"),
        Index(None, "primary_category", "event_time"),
        Index(None, "article_id"),
        Index(None, "event_time"),
        Index(None, "available_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_articles.id", ondelete="RESTRICT")
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("news_article_revisions.id", ondelete="RESTRICT")
    )
    taxonomy_version: Mapped[str] = mapped_column(String(32))
    classifier_version: Mapped[str] = mapped_column(String(48))
    rules_sha256: Mapped[str] = mapped_column(String(64))
    primary_category: Mapped[EventCategory] = mapped_column(
        str_enum(EventCategory, "event_category")
    )
    scope: Mapped[EventScope] = mapped_column(str_enum(EventScope, "event_scope"))
    # The provider's publication time of the revision (when the event was reported).
    event_time: Mapped[datetime]
    available_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EventClassificationRecord(Base):
    __tablename__ = "event_classifications"
    __table_args__ = (
        UniqueConstraint("event_id", "category", "source"),
        CheckConstraint("score >= 0", name="score_non_negative"),
        # Rule-based labels are estimates: never verified (D-076).
        CheckConstraint(
            "source <> 'ATLAS' OR verification = 'UNVERIFIED'", name="atlas_labels_unverified"
        ),
        Index(None, "category"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="RESTRICT"))
    category: Mapped[EventCategory] = mapped_column(str_enum(EventCategory, "event_category"))
    source: Mapped[ClassificationSource] = mapped_column(
        str_enum(ClassificationSource, "classification_source")
    )
    method: Mapped[ClassificationMethod] = mapped_column(
        str_enum(ClassificationMethod, "classification_method")
    )
    confidence: Mapped[ClassificationConfidence] = mapped_column(
        str_enum(ClassificationConfidence, "classification_confidence")
    )
    verification: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus, "verification_status")
    )
    is_primary: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[int] = mapped_column(Integer)
    provider_label: Mapped[str | None] = mapped_column(String(128))
    evidence: Mapped[dict[str, Any]] = mapped_column(server_default="{}")


class EventAssetRelationshipRecord(Base):
    __tablename__ = "event_asset_relationships"
    __table_args__ = (
        UniqueConstraint("event_id", "asset_id"),
        CheckConstraint(
            "static_relevance >= 0 AND static_relevance <= 1", name="relevance_in_unit_range"
        ),
        Index(None, "asset_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("news_asset_mappings.id", ondelete="RESTRICT")
    )
    relationship: Mapped[AssetRelationship] = mapped_column(
        str_enum(AssetRelationship, "news_asset_relationship")
    )
    relevance_version: Mapped[str] = mapped_column(String(32))
    static_relevance: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    relevance_factors: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    event_time: Mapped[datetime]
    available_at: Mapped[datetime]


class EconomicEventRecord(Base):
    """A scheduled economic release (identity and current state). Empty until a calendar
    provider is connected (D-077)."""

    __tablename__ = "economic_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id"),
        CheckConstraint("current_revision >= 1", name="revision_positive"),
        CheckConstraint("country IS NULL OR country ~ '^[A-Z]{2}$'", name="country_format"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_format"),
        Index(None, "scheduled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), ForeignKey(_PROVIDER_FK, ondelete="RESTRICT"))
    provider_event_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    country: Mapped[str | None] = mapped_column(String(2))
    currency: Mapped[str | None] = mapped_column(String(3))
    scheduled_at: Mapped[datetime]
    importance: Mapped[EconomicImportance] = mapped_column(
        str_enum(EconomicImportance, "economic_importance")
    )
    source: Mapped[str | None] = mapped_column(String(128))
    current_revision: Mapped[int] = mapped_column(Integer)
    first_received_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class EconomicEventRevisionRecord(Base):
    """Every distinct version of a scheduled release (forecast revisions, the actual value,
    later corrections), append-only, with availability as for news (D-075)."""

    __tablename__ = "economic_event_revisions"
    __table_args__ = (
        UniqueConstraint("event_id", "revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("available_at >= received_at", name="available_after_received"),
        Index(None, "available_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("economic_events.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    scheduled_at: Mapped[datetime]
    previous_value: Mapped[Decimal | None] = mapped_column(Numeric)
    previous_text: Mapped[str | None] = mapped_column(String(64))
    forecast_value: Mapped[Decimal | None] = mapped_column(Numeric)
    forecast_text: Mapped[str | None] = mapped_column(String(64))
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric)
    actual_text: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(32))
    importance: Mapped[EconomicImportance] = mapped_column(
        str_enum(EconomicImportance, "economic_importance")
    )
    provider_updated_at: Mapped[datetime | None]
    received_at: Mapped[datetime]
    available_at: Mapped[datetime]
    original_available_at: Mapped[datetime | None]
    provider_payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "EconomicEventRecord",
    "EconomicEventRevisionRecord",
    "EventAssetRelationshipRecord",
    "EventClassificationRecord",
    "NewsArticleRecord",
    "NewsArticleRevisionRecord",
    "NewsAssetMappingRecord",
    "NewsEventRecord",
    "NewsIngestionJobRecord",
    "NewsProviderRecord",
    "NewsSourceRecord",
    "NewsStreamSessionRecord",
    "NewsStreamStatusRecord",
]

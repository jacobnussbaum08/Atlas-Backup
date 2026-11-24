"""Market-data tables.

Reference (2A): ``market_data_providers`` and ``market_data_feeds`` give provider and
feed identity referential integrity. Rows are seeded by migrations and must match the
adapters' declared capabilities (enforced by an integration test).

Historical ingestion (2B):

* ``market_data_ingestion_jobs`` / ``..._job_symbols``: job state and per-symbol outcome.
* ``market_data_raw_events``: every provider response, verbatim. Append-only.
* ``market_bars``: normalized bars. Append-only; a changed re-observation of the same
  bar is a new row with ``revision + 1``, so earlier versions stay queryable.
* ``market_data_quality_events``: validation findings and limitations. Append-only.

Live streaming (2C):

* ``market_quotes`` / ``market_trades``: normalized live quotes and trades. Append-only;
  a repeated identical message is a duplicate (unique on time + content hash).
* ``market_bars`` also stores live bars (``source = LIVE_STREAM``) with their
  ``observation_kind``; updates from the provider are new revisions.
* ``market_data_stream_sessions``: one row per connection attempt (operational state).
* ``market_data_stream_status``: the current state of the worker per (provider, feed),
  heartbeat-updated, read by the health API.
* ``market_data_stream_gaps``: intervals in which live data may be missing, for the
  recovery workflow (Deliverable 2D).

Reliability and recovery (2D):

* ``market_data_coverage``: evidence that an interval of a series is complete (a full REST
  retrieval, or an uninterrupted live stream for bars). Gaps are expected data minus
  coverage, so a minute without a bar inside covered time means "no activity", not "lost".
* ``market_data_gaps``: missing intervals per asset/data type/timeframe with cause and
  status. Unresolved gaps of one series never overlap (exclusion constraint).
* ``market_data_recovery_jobs``: durable recovery of one gap through the 2B historical
  ingestion path; leases make interrupted jobs resumable.
* ``market_data_recovery_status``: heartbeat-updated state of the recovery worker.
* Live raw frames carry ``frame_seq``, unique per stream session, so a write retried
  after a lost commit acknowledgement can never store a frame twice.

Append-only tables are protected by database triggers (migration 0003). Prices and
volumes are unconstrained ``NUMERIC``: a fixed scale would silently round provider
values on insert.
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
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.data_engine.enums import (
    BarObservationKind,
    CoverageEvidence,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    DataType,
    FeedCoverage,
    GapCause,
    GapRecoveryStatus,
    GapStatus,
    IngestionJobStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
    RecoveryJobKind,
    RecoveryJobStatus,
    Severity,
    StreamGapKind,
)
from app.database.models.base import Base, str_enum

TIMEFRAME_PATTERN = "^[1-9][0-9]{0,2}(Min|Hour|Day|Week|Month)$"
_TIMEFRAME_FOR_BARS = "(data_type = 'BAR') = (timeframe IS NOT NULL)"
_COUNTERS_NON_NEGATIVE = (
    "LEAST(pages_retrieved, records_retrieved, records_validated, records_stored, "
    "records_duplicate, records_revised, records_rejected, records_quarantined) >= 0"
)


class MarketDataProviderRecord(Base):
    __tablename__ = "market_data_providers"
    __table_args__ = (CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,31}$'", name="code_format"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MarketDataFeedRecord(Base):
    __tablename__ = "market_data_feeds"
    __table_args__ = (
        CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,31}$'", name="code_format"),
        CheckConstraint("delay_seconds >= 0", name="delay_non_negative"),
    )

    provider: Mapped[str] = mapped_column(
        ForeignKey("market_data_providers.code", ondelete="RESTRICT"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    coverage: Mapped[FeedCoverage] = mapped_column(str_enum(FeedCoverage, "feed_coverage"))
    delay_seconds: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


def _feed_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["provider", "feed"],
        ["market_data_feeds.provider", "market_data_feeds.code"],
        name=f"fk_{table}_provider_feed_market_data_feeds",
        ondelete="RESTRICT",
    )


class _Counters:
    pages_retrieved: Mapped[int] = mapped_column(Integer, server_default="0")
    records_retrieved: Mapped[int] = mapped_column(Integer, server_default="0")
    records_validated: Mapped[int] = mapped_column(Integer, server_default="0")
    records_stored: Mapped[int] = mapped_column(Integer, server_default="0")
    records_duplicate: Mapped[int] = mapped_column(Integer, server_default="0")
    records_revised: Mapped[int] = mapped_column(Integer, server_default="0")
    records_rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    records_quarantined: Mapped[int] = mapped_column(Integer, server_default="0")


class IngestionJobRecord(_Counters, Base):
    __tablename__ = "market_data_ingestion_jobs"
    __table_args__ = (
        _feed_fk("market_data_ingestion_jobs"),
        CheckConstraint("range_end > range_start", name="range_ordered"),
        CheckConstraint(
            f"timeframe IS NULL OR timeframe ~ '{TIMEFRAME_PATTERN}'", name="timeframe_format"
        ),
        # Bars have a timeframe; trades and quotes (2D recovery) do not.
        CheckConstraint(_TIMEFRAME_FOR_BARS, name="timeframe_for_bars"),
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED', 'CANCELLED') "
            "OR completed_at IS NOT NULL",
            name="finished_has_completion_time",
        ),
        CheckConstraint(_COUNTERS_NON_NEGATIVE, name="counters_non_negative"),
        Index(None, "status", "created_at"),
        Index(None, "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    adjustment: Mapped[PriceAdjustment] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    data_type: Mapped[DataType] = mapped_column(str_enum(DataType, "data_type"))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    range_start: Mapped[datetime]
    range_end: Mapped[datetime]
    requested_symbols: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    status: Mapped[IngestionJobStatus] = mapped_column(
        str_enum(IngestionJobStatus, "ingestion_job_status"), server_default="PENDING"
    )
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class IngestionJobSymbolRecord(_Counters, Base):
    __tablename__ = "market_data_ingestion_job_symbols"
    __table_args__ = (
        CheckConstraint(_COUNTERS_NON_NEGATIVE, name="counters_non_negative"),
        Index(None, "asset_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_data_ingestion_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    provider_symbol: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    status: Mapped[IngestionSymbolStatus] = mapped_column(
        str_enum(IngestionSymbolStatus, "ingestion_symbol_status"), server_default="PENDING"
    )
    first_event_at: Mapped[datetime | None]
    last_event_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RawMarketDataEventRecord(Base):
    __tablename__ = "market_data_raw_events"
    __table_args__ = (
        _feed_fk("market_data_raw_events"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="payload_sha256_format"),
        Index(None, "ingestion_job_id"),
        Index(None, "received_at"),
        Index(None, "stream_session_id"),
        # A live frame is stored once per session, even if a write is retried after its
        # commit acknowledgement was lost (2D).
        UniqueConstraint("stream_session_id", "frame_seq", name="uq_market_data_raw_events_frame"),
        CheckConstraint(
            "stream_session_id IS NOT NULL OR frame_seq IS NULL", name="frame_seq_for_live_frames"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource, "data_source"))
    message_type: Mapped[str] = mapped_column(String(64))
    provider_symbol: Mapped[str | None] = mapped_column(String(128))
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_ingestion_jobs.id", ondelete="RESTRICT")
    )
    page_number: Mapped[int | None]
    stream_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_stream_sessions.id", ondelete="RESTRICT")
    )
    # 1-based order of the frame within its stream session (2D; NULL for 2C-era frames).
    frame_seq: Mapped[int | None] = mapped_column(BigInteger)
    request_params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    http_status: Mapped[int | None]
    received_at: Mapped[datetime]
    payload_text: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MarketBarRecord(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        _feed_fk("market_bars"),
        # Identity of one observation; also serves asset/timeframe/range queries.
        UniqueConstraint(
            "asset_id",
            "timeframe",
            "provider",
            "feed",
            "adjustment",
            "event_time",
            "revision",
            name="uq_market_bars_observation",
        ),
        CheckConstraint(f"timeframe ~ '{TIMEFRAME_PATTERN}'", name="timeframe_format"),
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        CheckConstraint("available_at >= received_at", name="available_after_received"),
        CheckConstraint(
            "source <> 'HISTORICAL_REST' OR original_available_at IS NULL",
            name="backfill_original_availability_unknown",
        ),
        CheckConstraint(
            "quality_status IN ('VALID', 'INCOMPLETE', 'QUARANTINED')",
            name="stored_quality_status",
        ),
        CheckConstraint(
            "(source = 'HISTORICAL_REST') = (observation_kind = 'HISTORICAL')",
            name="observation_kind_matches_source",
        ),
        Index(None, "ingestion_job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(128))
    timeframe: Mapped[str] = mapped_column(String(16))
    adjustment: Mapped[PriceAdjustment] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    event_time: Mapped[datetime]
    revision: Mapped[int] = mapped_column(Integer)
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal | None]
    vwap: Mapped[Decimal | None]
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    is_complete: Mapped[bool]
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource, "data_source"))
    observation_kind: Mapped[BarObservationKind] = mapped_column(
        str_enum(BarObservationKind, "bar_observation_kind"), server_default="HISTORICAL"
    )
    quality_status: Mapped[DataQualityStatus] = mapped_column(
        str_enum(DataQualityStatus, "data_quality_status")
    )
    received_at: Mapped[datetime]
    available_at: Mapped[datetime]
    original_available_at: Mapped[datetime | None]
    processed_at: Mapped[datetime]
    ingested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_ingestion_jobs.id", ondelete="RESTRICT")
    )
    raw_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_raw_events.id", ondelete="RESTRICT")
    )


class _LiveObservation:
    """Columns shared by live quotes and trades."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(128))
    # Truncated to microseconds; event_time_ns is the exact provider timestamp.
    event_time: Mapped[datetime]
    event_time_ns: Mapped[int] = mapped_column(BigInteger)
    conditions: Mapped[list[str]] = mapped_column(ARRAY(String(16)), server_default="{}")
    tape: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource, "data_source"))
    quality_status: Mapped[DataQualityStatus] = mapped_column(
        str_enum(DataQualityStatus, "data_quality_status")
    )
    # SHA-256 of the normalized content; with event_time_ns it identifies a repeat.
    content_sha256: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime]
    available_at: Mapped[datetime]
    original_available_at: Mapped[datetime | None]
    processed_at: Mapped[datetime]
    ingested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    raw_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_raw_events.id", ondelete="RESTRICT")
    )


def _live_args(table: str) -> tuple[Any, ...]:
    return (
        _feed_fk(table),
        UniqueConstraint(
            "asset_id",
            "provider",
            "feed",
            "event_time_ns",
            "content_sha256",
            name=f"uq_{table}_observation",
        ),
        CheckConstraint("available_at >= received_at", name="available_after_received"),
        CheckConstraint(
            "quality_status IN ('VALID', 'STALE', 'QUARANTINED')", name="stored_quality_status"
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_format"),
        CheckConstraint("event_time_ns >= 0", name="event_time_ns_non_negative"),
        Index(None, "asset_id", "feed", "event_time"),
        Index(None, "raw_event_id"),
    )


class MarketQuoteRecord(_LiveObservation, Base):
    __tablename__ = "market_quotes"
    __table_args__ = _live_args("market_quotes")

    bid_price: Mapped[Decimal | None]
    ask_price: Mapped[Decimal | None]
    # Provider units, unconverted (Alpaca documents round lots).
    bid_size: Mapped[Decimal | None]
    ask_size: Mapped[Decimal | None]
    bid_exchange: Mapped[str | None] = mapped_column(String(8))
    ask_exchange: Mapped[str | None] = mapped_column(String(8))


class MarketTradeRecord(_LiveObservation, Base):
    __tablename__ = "market_trades"
    __table_args__ = (*_live_args("market_trades"), Index(None, "provider_trade_id"))

    price: Mapped[Decimal]
    size: Mapped[Decimal | None]
    provider_trade_id: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(8))


class StreamSessionRecord(Base):
    """One connection attempt of a stream worker, from connect to close."""

    __tablename__ = "market_data_stream_sessions"
    __table_args__ = (
        _feed_fk("market_data_stream_sessions"),
        Index(None, "provider", "feed", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    worker_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(256))
    requested_subscription: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    confirmed_subscription: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    started_at: Mapped[datetime]
    authenticated_at: Mapped[datetime | None]
    subscribed_at: Mapped[datetime | None]
    first_message_at: Mapped[datetime | None]
    last_message_at: Mapped[datetime | None]
    last_persisted_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(Text)
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")


class StreamStatusRecord(Base):
    """Current state of the stream worker for one provider/feed (heartbeat-updated)."""

    __tablename__ = "market_data_stream_status"
    __table_args__ = (_feed_fk("market_data_stream_status"),)

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    feed: Mapped[str] = mapped_column(String(32), primary_key=True)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool]
    connection_state: Mapped[str] = mapped_column(String(32))
    auth_state: Mapped[str] = mapped_column(String(32))
    subscription_state: Mapped[str] = mapped_column(String(32))
    current_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_stream_sessions.id", ondelete="RESTRICT")
    )
    requested_subscription: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    confirmed_subscription: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    worker_started_at: Mapped[datetime | None]
    worker_stopped_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime]
    last_authenticated_at: Mapped[datetime | None]
    last_subscribed_at: Mapped[datetime | None]
    last_message_at: Mapped[datetime | None]
    last_persisted_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    recent_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    reconnect_count: Mapped[int] = mapped_column(Integer, server_default="0")
    queue_depth: Mapped[int] = mapped_column(Integer, server_default="0")
    queue_capacity: Mapped[int] = mapped_column(Integer, server_default="0")
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class StreamGapRecord(Base):
    """An interval in which live data may be missing. Closed (``gap_end``) once streaming
    resumes; backfilled by the recovery workflow (Deliverable 2D)."""

    __tablename__ = "market_data_stream_gaps"
    __table_args__ = (
        _feed_fk("market_data_stream_gaps"),
        CheckConstraint("gap_end IS NULL OR gap_end >= gap_start", name="gap_ordered"),
        Index(None, "provider", "feed", "gap_start"),
        Index(None, "recovery_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    kind: Mapped[StreamGapKind] = mapped_column(str_enum(StreamGapKind, "stream_gap_kind"))
    gap_start: Mapped[datetime]
    gap_end: Mapped[datetime | None]
    # Last frame received before the gap; data between it and gap_start may also be missing.
    last_message_before: Mapped[datetime | None]
    reason: Mapped[str] = mapped_column(Text)
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    previous_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_stream_sessions.id", ondelete="RESTRICT")
    )
    recovery_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_stream_sessions.id", ondelete="RESTRICT")
    )
    frames_dropped: Mapped[int] = mapped_column(Integer, server_default="0")
    recovery_status: Mapped[GapRecoveryStatus] = mapped_column(
        str_enum(GapRecoveryStatus, "gap_recovery_status"), server_default="PENDING"
    )
    detected_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]


class DataQualityEventRecord(Base):
    __tablename__ = "market_data_quality_events"
    __table_args__ = (
        CheckConstraint(
            f"timeframe IS NULL OR timeframe ~ '{TIMEFRAME_PATTERN}'", name="timeframe_format"
        ),
        Index(None, "detected_at"),
        Index(None, "ingestion_job_id"),
        Index(None, "asset_id", "detected_at"),
        Index(None, "event_type", "detected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_type: Mapped[DataQualityEventType] = mapped_column(
        str_enum(DataQualityEventType, "quality_event_type")
    )
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "severity"))
    provider: Mapped[str] = mapped_column(
        String(32), ForeignKey("market_data_providers.code", ondelete="RESTRICT")
    )
    feed: Mapped[str | None] = mapped_column(String(32))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider_symbol: Mapped[str | None] = mapped_column(String(128))
    data_type: Mapped[DataType | None] = mapped_column(str_enum(DataType, "data_type"))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    interval_start: Mapped[datetime | None]
    interval_end: Mapped[datetime | None]
    detected_at: Mapped[datetime]
    resulting_status: Mapped[DataQualityStatus | None] = mapped_column(
        str_enum(DataQualityStatus, "data_quality_status")
    )
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_ingestion_jobs.id", ondelete="RESTRICT")
    )
    raw_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_raw_events.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------- Deliverable 2D

_COUNTERS_2D = (
    "LEAST(attempts, records_retrieved, records_recovered, records_duplicate, "
    "records_revised, records_rejected) >= 0"
)


class CoverageRecord(Base):
    """Evidence that ``[covered_start, covered_end)`` of a series is complete.

    Bar series are keyed by timeframe and adjustment; trades/quotes have neither. Live
    segments are extended while the stream stays healthy (operational evidence, not
    market data, so the row is updated in place).
    """

    __tablename__ = "market_data_coverage"
    __table_args__ = (
        _feed_fk("market_data_coverage"),
        CheckConstraint("covered_end >= covered_start", name="interval_ordered"),
        CheckConstraint(_TIMEFRAME_FOR_BARS, name="timeframe_for_bars"),
        CheckConstraint(
            "(evidence = 'REST_RETRIEVAL') = (ingestion_job_id IS NOT NULL)",
            name="rest_evidence_has_job",
        ),
        CheckConstraint(
            "(evidence = 'LIVE_STREAM') = (stream_session_id IS NOT NULL)",
            name="live_evidence_has_session",
        ),
        Index(
            "ix_market_data_coverage_series",
            "asset_id",
            "data_type",
            "timeframe",
            "provider",
            "feed",
            "covered_start",
        ),
        Index(None, "stream_session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    data_type: Mapped[DataType] = mapped_column(str_enum(DataType, "data_type"))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    adjustment: Mapped[PriceAdjustment | None] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    covered_start: Mapped[datetime]
    covered_end: Mapped[datetime]
    evidence: Mapped[CoverageEvidence] = mapped_column(
        str_enum(CoverageEvidence, "coverage_evidence")
    )
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_ingestion_jobs.id", ondelete="RESTRICT")
    )
    stream_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_data_stream_sessions.id", ondelete="RESTRICT")
    )
    recorded_at: Mapped[datetime]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MarketDataGapRecord(Base):
    """A missing interval ``[gap_start, gap_end)`` of one series (bar start times for bars,
    event times for trades/quotes)."""

    __tablename__ = "market_data_gaps"
    __table_args__ = (
        _feed_fk("market_data_gaps"),
        CheckConstraint("gap_end > gap_start", name="interval_ordered"),
        CheckConstraint(_TIMEFRAME_FOR_BARS, name="timeframe_for_bars"),
        CheckConstraint(
            "status IN ('OPEN', 'RECOVERING') OR resolved_at IS NOT NULL",
            name="resolved_has_time",
        ),
        CheckConstraint(_COUNTERS_2D, name="counters_non_negative"),
        # Unresolved gaps of one series never overlap: overlapping detections are merged.
        ExcludeConstraint(
            ("asset_id", "="),
            ("provider", "="),
            ("feed", "="),
            ("data_type", "="),
            (text("coalesce(timeframe, '')"), "="),
            (text("tstzrange(gap_start, gap_end)"), "&&"),
            name="unresolved_gaps_disjoint",
            using="gist",
            where=text("status IN ('OPEN', 'RECOVERING')"),
        ),
        Index(None, "status", "gap_start"),
        Index(None, "asset_id", "data_type", "timeframe", "gap_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(128))
    data_type: Mapped[DataType] = mapped_column(str_enum(DataType, "data_type"))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    adjustment: Mapped[PriceAdjustment | None] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    gap_start: Mapped[datetime]
    gap_end: Mapped[datetime]
    cause: Mapped[GapCause] = mapped_column(str_enum(GapCause, "gap_cause"))
    status: Mapped[GapStatus] = mapped_column(
        str_enum(GapStatus, "gap_status"), server_default="OPEN"
    )
    stream_gap_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_stream_gaps.id", ondelete="RESTRICT")
    )
    parent_gap_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_gaps.id", ondelete="RESTRICT")
    )
    detected_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    records_retrieved: Mapped[int] = mapped_column(Integer, server_default="0")
    records_recovered: Mapped[int] = mapped_column(Integer, server_default="0")
    records_duplicate: Mapped[int] = mapped_column(Integer, server_default="0")
    records_revised: Mapped[int] = mapped_column(Integer, server_default="0")
    records_rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    resolution: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RecoveryJobRecord(Base):
    """Durable recovery (of a gap) or reconciliation (of a live-collected interval).

    Each attempt runs one 2B ingestion job; the job is finished only after the interval's
    completeness has been re-evaluated against the evidence that attempt produced.
    """

    __tablename__ = "market_data_recovery_jobs"
    __table_args__ = (
        _feed_fk("market_data_recovery_jobs"),
        CheckConstraint("(kind = 'RECOVERY') = (gap_id IS NOT NULL)", name="recovery_has_gap"),
        CheckConstraint("missing_end > missing_start", name="missing_ordered"),
        CheckConstraint("requested_end > requested_start", name="requested_ordered"),
        CheckConstraint(_TIMEFRAME_FOR_BARS, name="timeframe_for_bars"),
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED') "
            "OR completed_at IS NOT NULL",
            name="finished_has_completion_time",
        ),
        CheckConstraint(_COUNTERS_2D, name="counters_non_negative"),
        # At most one active job per gap: overlapping recovery requests share it.
        Index(
            "uq_market_data_recovery_jobs_active_gap",
            "gap_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        Index(None, "status", "next_attempt_at"),
        Index(None, "gap_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    kind: Mapped[RecoveryJobKind] = mapped_column(
        str_enum(RecoveryJobKind, "recovery_job_kind"), server_default="RECOVERY"
    )
    gap_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data_gaps.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(128))
    data_type: Mapped[DataType] = mapped_column(str_enum(DataType, "data_type"))
    timeframe: Mapped[str | None] = mapped_column(String(16))
    adjustment: Mapped[PriceAdjustment | None] = mapped_column(
        str_enum(PriceAdjustment, "price_adjustment")
    )
    missing_start: Mapped[datetime]
    missing_end: Mapped[datetime]
    requested_start: Mapped[datetime]
    requested_end: Mapped[datetime]
    status: Mapped[RecoveryJobStatus] = mapped_column(
        str_enum(RecoveryJobStatus, "recovery_job_status"), server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime]
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None]
    ingestion_job_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()), server_default="{}")
    records_retrieved: Mapped[int] = mapped_column(Integer, server_default="0")
    records_recovered: Mapped[int] = mapped_column(Integer, server_default="0")
    records_duplicate: Mapped[int] = mapped_column(Integer, server_default="0")
    records_revised: Mapped[int] = mapped_column(Integer, server_default="0")
    records_rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    # Sub-intervals still missing after the last attempt: [{"start": ..., "end": ...}].
    remaining: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RecoveryStatusRecord(Base):
    """Current state of the recovery worker for one provider/feed (heartbeat-updated)."""

    __tablename__ = "market_data_recovery_status"
    __table_args__ = (_feed_fk("market_data_recovery_status"),)

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    feed: Mapped[str] = mapped_column(String(32), primary_key=True)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool]
    state: Mapped[str] = mapped_column(String(32))
    heartbeat_at: Mapped[datetime]
    last_cycle_at: Mapped[datetime | None]
    last_successful_backfill_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    recent_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    counters: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

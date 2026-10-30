"""Standardized, provider-independent market-data models.

Two layers:

* ``Provider*`` records are what an adapter emits. They identify the instrument by
  the provider's symbol because adapters know nothing about Atlas's database.
* ``Market*`` records add Atlas's internal asset ID and a data-quality status.
  They are what the rest of Atlas consumes.

Validation here is structural: types, UTC, finite decimals, optional fields that
stay ``None`` when absent. Semantic checks (OHLC relationships, negative volume,
crossed quotes, staleness) belong to the data-quality engine, which can reject or
quarantine a record with an explanation instead of the record failing to parse.

Timestamps:
* ``event_time``: when the market event happened (for bars: the bar's start).
* ``received_at``: when Atlas received the record from the provider. For
  historical REST data this is the *retrieval* time.
* ``available_at``: when the record became available *to Atlas*; never earlier
  than ``received_at``. It answers "could Atlas have used this at time T?" when
  replaying Atlas's own past decisions (spec §7.4).
* ``original_available_at``: when the information was originally published in the
  market. Only observable for live data, where it is the time the provider delivered
  the message to Atlas (``received_at``). For REST backfill it is unknown and must
  stay ``None``: a retrieval time is never passed off as an original availability
  time. Backtests over backfilled data use the documented conservative policy in
  ``data_engine.availability`` instead.
* ``processed_at`` (``Market*`` records): when Atlas validated the record.
* Database persistence time is set by PostgreSQL, not by these models.

Live quotes and trades carry nanosecond timestamps. ``event_time`` holds them
truncated to microseconds (PostgreSQL's resolution); ``event_time_ns`` keeps the
exact value as integer nanoseconds since the Unix epoch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.clocks import ensure_utc
from app.data_engine.capabilities import DataType, FeedCoverage
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    IngestionJobStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
    Severity,
)
from app.data_engine.timeframes import TimeframeField
from app.database.models.asset import AssetClass, AssetStatus

UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_microseconds(value: datetime) -> int:
    """Exact microseconds since the Unix epoch (no float arithmetic)."""
    return (value - UNIX_EPOCH) // timedelta(microseconds=1)


# Finite decimals only: NaN and infinity are never valid market values.
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ProviderObservation(_Record):
    provider: str = Field(min_length=1)
    feed: str = Field(min_length=1)
    provider_symbol: str = Field(min_length=1)
    source: DataSource
    event_time: UtcDatetime
    received_at: UtcDatetime
    available_at: UtcDatetime
    original_available_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _availability_not_before_receipt(self) -> Self:
        if self.available_at < self.received_at:
            raise ValueError("available_at cannot be earlier than received_at")
        if self.source is DataSource.HISTORICAL_REST and self.original_available_at is not None:
            raise ValueError(
                "original_available_at is unknown for historical REST data and must be None"
            )
        return self


# ------------------------------------------------------------------ bars
class ProviderBar(_ProviderObservation):
    timeframe: TimeframeField
    # Required: adjusted and unadjusted prices are different series.
    adjustment: PriceAdjustment
    open: FiniteDecimal
    high: FiniteDecimal
    low: FiniteDecimal
    close: FiniteDecimal
    # None means the provider did not report volume. 0 means a genuine zero.
    volume: FiniteDecimal | None = None
    vwap: FiniteDecimal | None = None
    trade_count: int | None = None
    # False for bars that may still change (e.g. a running daily bar).
    is_complete: bool
    # Trading session label where the provider reports one (e.g. "REGULAR").
    session: str | None = None
    # 0 for the first stored observation; a changed re-observation is stored as revision+1.
    revision: int = Field(default=0, ge=0)
    observation_kind: BarObservationKind = BarObservationKind.HISTORICAL

    @model_validator(mode="after")
    def _kind_matches_source(self) -> Self:
        historical = self.observation_kind is BarObservationKind.HISTORICAL
        if historical != (self.source is DataSource.HISTORICAL_REST):
            raise ValueError(
                f"observation_kind {self.observation_kind.value} does not match "
                f"source {self.source.value}"
            )
        return self


class MarketBar(ProviderBar):
    asset_id: uuid.UUID
    quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED
    processed_at: UtcDatetime | None = None

    @classmethod
    def from_provider(
        cls,
        bar: ProviderBar,
        asset_id: uuid.UUID,
        quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED,
        processed_at: datetime | None = None,
    ) -> MarketBar:
        return cls(
            **bar.model_dump(),
            asset_id=asset_id,
            quality_status=quality_status,
            processed_at=processed_at,
        )


class _NanosecondObservation(_ProviderObservation):
    # Exact event time in nanoseconds since the Unix epoch, where the provider reports it.
    event_time_ns: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _nanoseconds_match_event_time(self) -> Self:
        exact = self.event_time_ns
        if exact is not None and exact // 1000 != epoch_microseconds(self.event_time):
            raise ValueError("event_time_ns does not match event_time")
        return self


# ------------------------------------------------------------------ quotes
class ProviderQuote(_NanosecondObservation):
    bid_price: FiniteDecimal | None = None
    ask_price: FiniteDecimal | None = None
    bid_size: FiniteDecimal | None = None
    ask_size: FiniteDecimal | None = None
    bid_exchange: str | None = None
    ask_exchange: str | None = None
    conditions: tuple[str, ...] = ()
    tape: str | None = None


class MarketQuote(ProviderQuote):
    asset_id: uuid.UUID
    quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED

    @classmethod
    def from_provider(
        cls,
        quote: ProviderQuote,
        asset_id: uuid.UUID,
        quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED,
    ) -> MarketQuote:
        return cls(**quote.model_dump(), asset_id=asset_id, quality_status=quality_status)


# ------------------------------------------------------------------ trades
class ProviderTrade(_NanosecondObservation):
    price: FiniteDecimal
    size: FiniteDecimal | None = None
    provider_trade_id: str | None = None
    exchange: str | None = None
    conditions: tuple[str, ...] = ()
    tape: str | None = None


class MarketTrade(ProviderTrade):
    asset_id: uuid.UUID
    quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED

    @classmethod
    def from_provider(
        cls,
        trade: ProviderTrade,
        asset_id: uuid.UUID,
        quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED,
    ) -> MarketTrade:
        return cls(**trade.model_dump(), asset_id=asset_id, quality_status=quality_status)


# ------------------------------------------------------------------ raw events
class MarketDataEvent(_Record):
    """An immutable raw message as received from a provider, kept for audit and replay."""

    provider: str
    feed: str
    source: DataSource
    message_type: str  # provider's own type code, e.g. Alpaca "b", "q", "t", "error"
    data_type: DataType | None = None  # None for control messages
    provider_symbol: str | None = None
    provider_event_id: str | None = None
    event_time: UtcDatetime | None = None
    received_at: UtcDatetime
    payload: dict[str, Any]
    # Exact response text, stored verbatim so no numeric precision is lost.
    raw_body: str | None = None
    # Request parameters that produced the message. Never contains credentials.
    request: dict[str, str] = Field(default_factory=dict)
    http_status: int | None = None


# ------------------------------------------------------------------ assets
class MarketAsset(_Record):
    asset_id: uuid.UUID
    symbol: str
    asset_class: AssetClass
    exchange_code: str | None = None
    quote_currency: str
    status: AssetStatus
    provider: str | None = None
    provider_symbol: str | None = None


# ------------------------------------------------------------------ provider status
class ConnectionState(StrEnum):
    """Transport/session state of a stream. Authentication and subscription are tracked
    separately (``StreamAuthState``/``StreamSubscriptionState``): an open socket proves
    neither."""

    DISABLED = "DISABLED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"  # open and authenticated, not (yet) subscribed
    SUBSCRIBING = "SUBSCRIBING"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"  # closed on request (graceful shutdown)
    FAILED = "FAILED"  # permanent failure; not retrying


class CredentialStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"  # present and well-formed; not yet checked with the provider
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class ProviderStatus(_Record):
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    credentials: CredentialStatus
    stream_state: ConnectionState
    checked_at: UtcDatetime
    last_event_at: UtcDatetime | None = None
    last_successful_retrieval_at: UtcDatetime | None = None
    last_error: str | None = None  # redacted, single line
    subscribed_symbols: tuple[str, ...] = ()


# ------------------------------------------------------------------ data quality


class DataQualityEvent(_Record):
    event_type: DataQualityEventType
    severity: Severity
    provider: str
    feed: str | None = None
    asset_id: uuid.UUID | None = None
    provider_symbol: str | None = None
    data_type: DataType | None = None
    timeframe: TimeframeField | None = None
    interval_start: UtcDatetime | None = None
    interval_end: UtcDatetime | None = None
    detected_at: UtcDatetime
    resulting_status: DataQualityStatus | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ ingestion jobs


class IngestionCounters(_Record):
    """Counting rules:

    * retrieved = bars received from the provider (all pages)
    * rejected = not stored (unparseable, wrong symbol, outside the requested range,
      or conflicting duplicate inside one response)
    * validated = retrieved - rejected (checked by the quality engine)
    * duplicate = identical to the latest stored revision; not stored again
    * stored = new rows written (first observations + revisions), whatever their status
    * revised = subset of stored: rows that are a new revision of an existing bar
    * quarantined = subset of stored: rows stored with status QUARANTINED
    """

    pages_retrieved: int = Field(default=0, ge=0)
    records_retrieved: int = Field(default=0, ge=0)
    records_validated: int = Field(default=0, ge=0)
    records_stored: int = Field(default=0, ge=0)
    records_duplicate: int = Field(default=0, ge=0)
    records_revised: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    records_quarantined: int = Field(default=0, ge=0)


class IngestionSymbolResult(_Record):
    provider_symbol: str
    asset_id: uuid.UUID
    status: IngestionSymbolStatus
    counters: IngestionCounters
    first_event_at: UtcDatetime | None = None
    last_event_at: UtcDatetime | None = None
    error: str | None = None


class DataIngestionJob(_Record):
    job_id: uuid.UUID
    provider: str
    feed: str
    adjustment: PriceAdjustment
    data_type: DataType
    provider_symbols: tuple[str, ...] = Field(min_length=1)
    timeframe: TimeframeField | None = None
    range_start: UtcDatetime
    range_end: UtcDatetime
    status: IngestionJobStatus
    counters: IngestionCounters = Field(default_factory=IngestionCounters)
    symbols: tuple[IngestionSymbolResult, ...] = ()
    error: str | None = None
    requested_by: str | None = None
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _range_ordered(self) -> Self:
        if self.range_end <= self.range_start:
            raise ValueError("range_end must be after range_start")
        return self


__all__ = [
    "ConnectionState",
    "CredentialStatus",
    "DataIngestionJob",
    "DataQualityEvent",
    "DataQualityEventType",
    "DataQualityStatus",
    "DataSource",
    "IngestionCounters",
    "IngestionJobStatus",
    "IngestionSymbolResult",
    "IngestionSymbolStatus",
    "MarketAsset",
    "MarketBar",
    "MarketDataEvent",
    "MarketQuote",
    "MarketTrade",
    "ProviderBar",
    "ProviderQuote",
    "ProviderStatus",
    "ProviderTrade",
    "Severity",
    "UtcDatetime",
]

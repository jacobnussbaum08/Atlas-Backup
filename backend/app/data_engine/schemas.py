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
* ``received_at``: when Atlas received the record from the provider.
* ``available_at``: when the record became usable for decisions; never earlier than
  ``received_at``. Historical simulations may only use records with
  ``available_at <= decision time`` (spec §7.4).
* Database persistence time is set by PostgreSQL, not by these models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.clocks import ensure_utc
from app.data_engine.capabilities import DataType, FeedCoverage
from app.data_engine.timeframes import TimeframeField
from app.database.models.asset import AssetClass, AssetStatus

UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
# Finite decimals only: NaN and infinity are never valid market values.
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class DataSource(StrEnum):
    """Provenance of a record. Historical and live data share one schema."""

    HISTORICAL_REST = "HISTORICAL_REST"
    LIVE_STREAM = "LIVE_STREAM"


class DataQualityStatus(StrEnum):
    UNCHECKED = "UNCHECKED"
    VALID = "VALID"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    QUARANTINED = "QUARANTINED"


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

    @model_validator(mode="after")
    def _availability_not_before_receipt(self) -> Self:
        if self.available_at < self.received_at:
            raise ValueError("available_at cannot be earlier than received_at")
        return self


# ------------------------------------------------------------------ bars
class ProviderBar(_ProviderObservation):
    timeframe: TimeframeField
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
    # 0 for the first observation; a provider update/correction increments it.
    revision: int = Field(default=0, ge=0)


class MarketBar(ProviderBar):
    asset_id: uuid.UUID
    quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED

    @classmethod
    def from_provider(
        cls,
        bar: ProviderBar,
        asset_id: uuid.UUID,
        quality_status: DataQualityStatus = DataQualityStatus.UNCHECKED,
    ) -> MarketBar:
        return cls(**bar.model_dump(), asset_id=asset_id, quality_status=quality_status)


# ------------------------------------------------------------------ quotes
class ProviderQuote(_ProviderObservation):
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
class ProviderTrade(_ProviderObservation):
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
    DISABLED = "DISABLED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


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
class DataQualityEventType(StrEnum):
    MISSING_INTERVAL = "MISSING_INTERVAL"
    STALE_DATA = "STALE_DATA"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    OUT_OF_ORDER_EVENT = "OUT_OF_ORDER_EVENT"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_OHLC = "INVALID_OHLC"
    INVALID_QUOTE = "INVALID_QUOTE"
    MISSING_FIELD = "MISSING_FIELD"
    REJECTED_MESSAGE = "REJECTED_MESSAGE"
    UNEXPECTED_SYMBOL = "UNEXPECTED_SYMBOL"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    RECOVERY = "RECOVERY"
    CORRECTION = "CORRECTION"
    CAPABILITY_LIMITATION = "CAPABILITY_LIMITATION"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


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
class IngestionJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"  # finished, but some symbols/ranges are missing or failed
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DataIngestionJob(_Record):
    job_id: uuid.UUID
    provider: str
    feed: str
    data_type: DataType
    provider_symbols: tuple[str, ...] = Field(min_length=1)
    timeframe: TimeframeField | None = None
    range_start: UtcDatetime
    range_end: UtcDatetime
    status: IngestionJobStatus
    records_retrieved: int = Field(default=0, ge=0)
    records_stored: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    records_duplicate: int = Field(default=0, ge=0)
    missing_symbols: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
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
    "IngestionJobStatus",
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

"""The market-data provider contract.

Every provider adapter implements :class:`MarketDataProvider`. The rest of Atlas
depends only on this module, the capability model and the standardized schemas;
nothing outside ``app.data_engine.providers.<name>`` may import adapter internals
or parse provider-specific payloads.

Contract rules every adapter must follow:

* Never fall back to another feed. Requests always name the configured feed
  explicitly, and an unsupported or unentitled feed raises
  ``UnsupportedCapabilityError``.
* Never fabricate data. Empty or partial provider responses are returned as such.
* Never include credential values in exceptions, logs, ``repr`` or status output.
* Convert provider timestamps to timezone-aware UTC, and provider numbers to ``Decimal``.
* Market data only: adapters never call brokerage or order endpoints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import MarketDataSettings
from app.data_engine.capabilities import FeedAccess, FeedCoverage, ProviderCapabilities
from app.data_engine.enums import (
    DataType,
    PriceAdjustment,
    StreamAuthState,
    StreamChannel,
    StreamSubscriptionState,
)
from app.data_engine.errors import ProviderConfigurationError, UnsupportedCapabilityError
from app.data_engine.schemas import (
    ConnectionState,
    CredentialStatus,
    MarketDataEvent,
    ProviderBar,
    ProviderQuote,
    ProviderTrade,
    Severity,
    UtcDatetime,
)
from app.data_engine.timeframes import TimeframeField


# ------------------------------------------------------------------ requests and results
class HistoricalBarsRequest(BaseModel):
    """One historical bar retrieval. ``start`` and ``end`` are inclusive bar start times."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_symbols: tuple[str, ...] = Field(min_length=1)
    timeframe: TimeframeField
    start: UtcDatetime
    end: UtcDatetime
    # Explicit: adjusted and unadjusted prices are different series.
    adjustment: PriceAdjustment
    page_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if len(set(self.provider_symbols)) != len(self.provider_symbols):
            raise ValueError("provider_symbols contains duplicates")
        return self


class HistoricalObservationsRequest(BaseModel):
    """Historical trades or quotes (Deliverable 2D recovery). ``start``/``end`` are
    inclusive event times."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_symbols: tuple[str, ...] = Field(min_length=1)
    data_type: DataType
    start: UtcDatetime
    end: UtcDatetime
    page_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.data_type is DataType.BAR:
            raise ValueError("bars use HistoricalBarsRequest")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if len(set(self.provider_symbols)) != len(self.provider_symbols):
            raise ValueError("provider_symbols contains duplicates")
        return self


class MalformedRecord(BaseModel):
    """A provider item that could not be normalized. Reported, never silently dropped."""

    model_config = ConfigDict(frozen=True)

    provider_symbol: str | None
    reason: str
    item: dict[str, Any]


class BarPage(BaseModel):
    """One page of a historical response, exactly as the provider returned it (normalized)."""

    model_config = ConfigDict(frozen=True)

    page_number: int = Field(ge=1)
    bars: tuple[ProviderBar, ...]
    malformed: tuple[MalformedRecord, ...] = ()
    currency: str | None = None
    next_page_token: str | None
    requested_at: UtcDatetime
    received_at: UtcDatetime
    raw_event: MarketDataEvent


class ObservationPage(BaseModel):
    """One page of historical trades or quotes (normalized), with its verbatim response."""

    model_config = ConfigDict(frozen=True)

    page_number: int = Field(ge=1)
    data_type: DataType
    trades: tuple[ProviderTrade, ...] = ()
    quotes: tuple[ProviderQuote, ...] = ()
    malformed: tuple[MalformedRecord, ...] = ()
    next_page_token: str | None
    requested_at: UtcDatetime
    received_at: UtcDatetime
    raw_event: MarketDataEvent

    @property
    def item_count(self) -> int:
        return len(self.trades) + len(self.quotes) + len(self.malformed)


class StreamSubscription(BaseModel):
    """Provider symbols per live channel."""

    model_config = ConfigDict(frozen=True)

    bars: frozenset[str] = frozenset()
    updated_bars: frozenset[str] = frozenset()
    daily_bars: frozenset[str] = frozenset()
    quotes: frozenset[str] = frozenset()
    trades: frozenset[str] = frozenset()

    @classmethod
    def of(cls, channels: frozenset[StreamChannel], symbols: frozenset[str]) -> StreamSubscription:
        return cls(**{channel.value: symbols for channel in channels})

    def channel(self, channel: StreamChannel) -> frozenset[str]:
        symbols: frozenset[str] = getattr(self, channel.value)
        return symbols

    @property
    def channels(self) -> frozenset[StreamChannel]:
        return frozenset(c for c in StreamChannel if self.channel(c))

    @property
    def is_empty(self) -> bool:
        return not self.channels

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset().union(*(self.channel(c) for c in StreamChannel))

    def missing_from(self, confirmed: StreamSubscription) -> dict[StreamChannel, frozenset[str]]:
        """Channel -> symbols requested here but absent from ``confirmed``."""
        missing = {c: self.channel(c) - confirmed.channel(c) for c in StreamChannel}
        return {c: symbols for c, symbols in missing.items() if symbols}

    def as_dict(self) -> dict[str, list[str]]:
        return {c.value: sorted(self.channel(c)) for c in StreamChannel if self.channel(c)}


class ConfigurationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: Severity
    code: str
    message: str


class ProviderConfigurationReport(BaseModel):
    """Result of offline configuration checks. Contains no credential values."""

    model_config = ConfigDict(frozen=True)

    provider: str
    feed: str
    feed_coverage: FeedCoverage
    credentials: CredentialStatus
    stream_enabled: bool
    issues: tuple[ConfigurationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ConfigurationIssue, ...]:
        return tuple(i for i in self.issues if i.severity in (Severity.ERROR, Severity.CRITICAL))

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            details = "; ".join(f"{i.code}: {i.message}" for i in self.errors)
            raise ProviderConfigurationError(f"{self.provider} configuration invalid: {details}")


class AuthenticationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    status: CredentialStatus
    checked_at: UtcDatetime
    detail: str | None = None  # redacted, single line


# ------------------------------------------------------------------ streaming contract
class StreamNotice(BaseModel):
    """A provider message that is not an observation but must not be lost.

    Examples: a trade correction or cancellation, a trading-status message, or a
    control message the adapter recognised but does not act on.
    """

    model_config = ConfigDict(frozen=True)

    kind: str  # e.g. "trade_correction", "trade_cancel", "unknown_message"
    provider_symbol: str | None = None
    event_time: UtcDatetime | None = None
    data_type: DataType | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StreamBatch(BaseModel):
    """Everything decoded from one received frame.

    ``raw_event`` is the frame verbatim. Every item in the frame ends up in exactly
    one of ``bars``/``quotes``/``trades``/``notices``/``malformed``: nothing is
    dropped silently.
    """

    model_config = ConfigDict(frozen=True)

    raw_event: MarketDataEvent
    bars: tuple[ProviderBar, ...] = ()
    quotes: tuple[ProviderQuote, ...] = ()
    trades: tuple[ProviderTrade, ...] = ()
    notices: tuple[StreamNotice, ...] = ()
    malformed: tuple[MalformedRecord, ...] = ()

    @property
    def item_count(self) -> int:
        return (
            len(self.bars)
            + len(self.quotes)
            + len(self.trades)
            + len(self.notices)
            + len(self.malformed)
        )


class MarketDataStream(Protocol):
    """A persistent provider stream. Owned by a background worker, never by a request.

    Connection, authentication and subscription are separate states: an open socket
    proves neither authentication nor subscription. Methods raise ``StreamError``
    subclasses; ``permanent`` errors must not be retried automatically.
    """

    @property
    def state(self) -> ConnectionState: ...

    @property
    def auth_state(self) -> StreamAuthState: ...

    @property
    def subscription_state(self) -> StreamSubscriptionState: ...

    @property
    def subscription(self) -> StreamSubscription:
        """Subscription as last confirmed by the provider."""
        ...

    async def connect(self) -> None:
        """Open the connection and authenticate; returns only once authentication is confirmed."""
        ...

    async def subscribe(self, subscription: StreamSubscription) -> StreamSubscription:
        """Request ``subscription``; returns the provider's confirmed subscription."""
        ...

    async def unsubscribe(self, subscription: StreamSubscription) -> StreamSubscription: ...

    def events(self) -> AsyncIterator[StreamBatch]:
        """Decoded frames until the connection closes (raises ``StreamError`` if unexpected)."""
        ...

    async def close(self) -> None: ...


# ------------------------------------------------------------------ provider contract
class MarketDataProvider(ABC):
    """A market-data source (never a broker)."""

    provider_id: ClassVar[str]
    display_name: ClassVar[str]

    @property
    @abstractmethod
    def feed(self) -> str:
        """The single configured feed. Requests never use any other feed implicitly."""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """What this adapter can deliver, independent of the subscription plan."""

    @abstractmethod
    def feed_access(self, feed: str | None = None) -> FeedAccess:
        """What the configured subscription is entitled to for ``feed`` (default: configured)."""

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str:
        """Validate and convert an Atlas/provider symbol to the provider's request format."""

    @abstractmethod
    def credential_status(self) -> CredentialStatus:
        """Offline credential status (``NOT_CONFIGURED`` or ``CONFIGURED``)."""

    @abstractmethod
    def validate_configuration(self, settings: MarketDataSettings) -> ProviderConfigurationReport:
        """Offline checks of feed, plan, symbols and limits. Performs no network I/O."""

    @abstractmethod
    def validate_historical_request(self, request: HistoricalBarsRequest) -> None:
        """Offline check that ``request`` is supported and entitled on the configured feed.

        Raises ``UnsupportedCapabilityError`` (or a subclass) otherwise. No network I/O.
        """

    @abstractmethod
    async def check_authentication(self) -> AuthenticationResult:
        """Verify credentials with the provider using a market-data request."""

    @abstractmethod
    def fetch_bars(self, request: HistoricalBarsRequest) -> AsyncIterator[BarPage]:
        """Yield pages of historical bars on the configured feed, following pagination."""

    def validate_observations_request(self, request: HistoricalObservationsRequest) -> None:
        """Offline check that historical trades/quotes are supported and entitled."""
        raise UnsupportedCapabilityError(
            f"{self.display_name} adapter does not provide historical "
            f"{request.data_type.value.lower()}s"
        )

    def fetch_observations(
        self, request: HistoricalObservationsRequest
    ) -> AsyncIterator[ObservationPage]:
        """Yield pages of historical trades or quotes on the configured feed (2D)."""
        raise UnsupportedCapabilityError(
            f"{self.display_name} adapter does not provide historical "
            f"{request.data_type.value.lower()}s"
        )

    @abstractmethod
    def create_stream(self) -> MarketDataStream:
        """Create (but do not connect) a live stream on the configured feed."""

    def streaming_channels(self) -> frozenset[StreamChannel]:
        """Live channels this adapter implements on the configured feed."""
        return frozenset()

    async def aclose(self) -> None:  # noqa: B027  # optional hook; default has nothing to release
        """Release network resources."""

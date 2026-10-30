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
from typing import ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import MarketDataSettings
from app.data_engine.capabilities import FeedAccess, FeedCoverage, ProviderCapabilities
from app.data_engine.errors import ProviderConfigurationError
from app.data_engine.schemas import (
    ConnectionState,
    CredentialStatus,
    MarketDataEvent,
    ProviderBar,
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
    page_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if len(set(self.provider_symbols)) != len(self.provider_symbols):
            raise ValueError("provider_symbols contains duplicates")
        return self


class BarPage(BaseModel):
    """One page of a historical response, exactly as the provider returned it (normalized)."""

    model_config = ConfigDict(frozen=True)

    bars: tuple[ProviderBar, ...]
    next_page_token: str | None
    requested_at: UtcDatetime
    received_at: UtcDatetime
    raw_event: MarketDataEvent


class StreamSubscription(BaseModel):
    """Provider symbols per live channel."""

    model_config = ConfigDict(frozen=True)

    bars: frozenset[str] = frozenset()
    quotes: frozenset[str] = frozenset()
    trades: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not (self.bars or self.quotes or self.trades)

    @property
    def symbols(self) -> frozenset[str]:
        return self.bars | self.quotes | self.trades


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
class MarketDataStream(Protocol):
    """A persistent provider stream. Owned by a background worker, never by a request."""

    @property
    def state(self) -> ConnectionState: ...

    @property
    def subscription(self) -> StreamSubscription:
        """Subscription confirmed by the provider (restored after reconnects)."""
        ...

    async def connect(self) -> None: ...

    async def subscribe(self, subscription: StreamSubscription) -> StreamSubscription: ...

    async def unsubscribe(self, subscription: StreamSubscription) -> StreamSubscription: ...

    def events(self) -> AsyncIterator[MarketDataEvent]: ...

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
    async def check_authentication(self) -> AuthenticationResult:
        """Verify credentials with the provider using a market-data request."""

    @abstractmethod
    def fetch_bars(self, request: HistoricalBarsRequest) -> AsyncIterator[BarPage]:
        """Yield pages of historical bars on the configured feed, following pagination."""

    @abstractmethod
    def create_stream(self) -> MarketDataStream:
        """Create (but do not connect) a live stream on the configured feed."""

    async def aclose(self) -> None:  # noqa: B027  # optional hook; default has nothing to release
        """Release network resources."""

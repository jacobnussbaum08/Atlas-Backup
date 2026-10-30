"""Alpaca market-data adapter (Deliverable 2A foundation).

Implemented now (no network I/O): identification, capabilities, plan entitlements,
symbol normalization, offline configuration validation, authentication header and
historical-request construction, and stream URL selection.

Network operations raise ``ProviderOperationNotImplementedError`` until their
deliverable lands: historical retrieval and authentication checks in 2B, live
streaming in 2C. Nothing here returns placeholder or fabricated data.

Only Alpaca's market-data hosts are used. Trading and brokerage endpoints are
never contacted.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

from app.core.clocks import Clock, SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.capabilities import DataType, FeedAccess, FeedCoverage, ProviderCapabilities
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    InvalidProviderSymbolError,
    ProviderOperationNotImplementedError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.capabilities import (
    ALPACA_CAPABILITIES,
    DISPLAY_NAME,
    PROVIDER_ID,
    feed_access,
    parse_feed,
)
from app.data_engine.providers.alpaca.config import AlpacaFeed, AlpacaSettings, get_alpaca_settings
from app.data_engine.providers.base import (
    AuthenticationResult,
    BarPage,
    ConfigurationIssue,
    HistoricalBarsRequest,
    MarketDataProvider,
    MarketDataStream,
    ProviderConfigurationReport,
)
from app.data_engine.schemas import CredentialStatus, Severity
from app.data_engine.timeframes import Timeframe

DATA_API_BASE_URL = "https://data.alpaca.markets"
STREAM_BASE_URL = "wss://stream.data.alpaca.markets"
HISTORICAL_BARS_PATH = "/v2/stocks/bars"
_STREAM_PATHS = {
    AlpacaFeed.IEX: "/v2/iex",
    AlpacaFeed.SIP: "/v2/sip",
    AlpacaFeed.DELAYED_SIP: "/v2/delayed_sip",
}
KEY_HEADER = "APCA-API-KEY-ID"
SECRET_HEADER = "APCA-API-SECRET-KEY"  # noqa: S105  # header name, not a credential

# US equity/ETF symbols as Alpaca writes them, e.g. AAPL, BRK.B.
_SYMBOL = re.compile(r"^[A-Z0-9]{1,10}(\.[A-Z0-9]{1,4})?$")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AlpacaMarketDataProvider(MarketDataProvider):
    provider_id: ClassVar[str] = PROVIDER_ID
    display_name: ClassVar[str] = DISPLAY_NAME

    def __init__(self, settings: AlpacaSettings, clock: Clock | None = None) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()

    @classmethod
    def from_environment(cls, clock: Clock) -> AlpacaMarketDataProvider:
        return cls(get_alpaca_settings(), clock)

    def __repr__(self) -> str:
        plan = self._settings.data_plan.value
        return (
            f"AlpacaMarketDataProvider(feed={self.feed!r}, plan={plan!r}, "
            f"credentials={self.credential_status().value})"
        )

    # ------------------------------------------------------------ identity and capabilities
    @property
    def feed(self) -> str:
        return self._settings.data_feed.value

    def capabilities(self) -> ProviderCapabilities:
        return ALPACA_CAPABILITIES

    def feed_access(self, feed: str | None = None) -> FeedAccess:
        return feed_access(parse_feed(feed or self.feed), self._settings.data_plan)

    def normalize_symbol(self, symbol: str) -> str:
        candidate = symbol.strip().upper()
        if not _SYMBOL.match(candidate):
            raise InvalidProviderSymbolError(f"'{symbol}' is not a valid Alpaca equity symbol")
        return candidate

    def credential_status(self) -> CredentialStatus:
        if self._settings.credentials_configured:
            return CredentialStatus.CONFIGURED
        return CredentialStatus.NOT_CONFIGURED

    # ------------------------------------------------------------ offline validation
    def validate_configuration(self, settings: MarketDataSettings) -> ProviderConfigurationReport:
        issues: list[ConfigurationIssue] = []

        def issue(severity: Severity, code: str, message: str) -> None:
            issues.append(ConfigurationIssue(severity=severity, code=code, message=message))

        access = self.feed_access()
        stream = settings.market_data_stream_enabled

        if settings.market_data_provider != self.provider_id:
            issue(Severity.ERROR, "provider_mismatch", "configured provider is not alpaca")

        if self.credential_status() is CredentialStatus.NOT_CONFIGURED:
            issue(
                Severity.ERROR if stream else Severity.WARNING,
                "credentials_not_configured",
                "ALPACA_API_KEY/ALPACA_SECRET_KEY are not set; Alpaca market-data requests "
                "require authentication",
            )

        if access.coverage is FeedCoverage.SINGLE_VENUE:
            issue(Severity.WARNING, "single_venue_feed", "; ".join(access.notes))
        elif access.notes:
            issue(Severity.INFO, "feed_entitlement", "; ".join(access.notes))

        invalid = []
        for symbol in settings.market_data_symbols:
            try:
                self.normalize_symbol(symbol)
            except InvalidProviderSymbolError:
                invalid.append(symbol)
        if invalid:
            issue(
                Severity.ERROR,
                "invalid_symbols",
                f"not valid Alpaca equity symbols: {', '.join(invalid)}",
            )

        max_page = ALPACA_CAPABILITIES.max_page_size
        if max_page is not None and settings.historical_data_batch_size > max_page:
            issue(
                Severity.ERROR,
                "batch_size_too_large",
                f"HISTORICAL_DATA_BATCH_SIZE exceeds Alpaca's maximum page size of {max_page}",
            )

        if not access.historical:
            issue(
                Severity.WARNING,
                "feed_without_history",
                f"feed '{self.feed}' provides no historical data on this plan",
            )

        if stream:
            if not access.streaming:
                issue(
                    Severity.ERROR,
                    "feed_not_streamable",
                    f"feed '{self.feed}' cannot be streamed on the "
                    f"'{self._settings.data_plan.value}' plan",
                )
            if not settings.market_data_symbols:
                issue(Severity.ERROR, "no_symbols", "streaming is enabled but no symbols are set")
            limit = access.stream_symbol_limit
            if limit is not None and len(settings.market_data_symbols) > limit:
                issue(
                    Severity.ERROR,
                    "stream_symbol_limit",
                    f"{len(settings.market_data_symbols)} symbols exceed the plan's "
                    f"stream limit of {limit}",
                )
        else:
            issue(Severity.INFO, "stream_disabled", "live streaming is disabled")

        return ProviderConfigurationReport(
            provider=self.provider_id,
            feed=self.feed,
            feed_coverage=access.coverage,
            credentials=self.credential_status(),
            stream_enabled=stream,
            issues=tuple(issues),
        )

    # ------------------------------------------------------------ request construction
    def authentication_headers(self) -> dict[str, str]:
        """HTTP/WebSocket auth headers. Contains secrets: never log or return to clients."""
        key, secret = self._settings.api_key, self._settings.secret_key
        if key is None or secret is None:
            raise CredentialsNotConfiguredError("Alpaca credentials are not configured")
        return {KEY_HEADER: key.get_secret_value(), SECRET_HEADER: secret.get_secret_value()}

    def provider_timeframe(self, timeframe: Timeframe) -> str:
        if not ALPACA_CAPABILITIES.supports_historical_timeframe(timeframe):
            raise UnsupportedCapabilityError(f"Alpaca does not provide {timeframe} bars")
        return str(timeframe)  # Atlas's canonical form ("5Min", "1Hour", "1Day") is Alpaca's

    def historical_bars_query(
        self, request: HistoricalBarsRequest, page_token: str | None = None
    ) -> dict[str, str]:
        """Query parameters for ``GET /v2/stocks/bars``.

        The feed is always sent explicitly: Alpaca's default is ``sip``, and Atlas must
        never receive a different feed than the one it records. Prices are requested
        unadjusted (``raw``); corporate-action handling is a separate concern.
        """
        access = self.feed_access()
        access.require_historical()
        ALPACA_CAPABILITIES.require_historical_bars(self.feed, request.timeframe)
        if access.historical_recency_limit is not None:
            newest_allowed = self._clock.now() - access.historical_recency_limit
            if request.end > newest_allowed:
                raise UnsupportedCapabilityError(
                    f"feed '{self.feed}' on the '{self._settings.data_plan.value}' plan has no "
                    f"data newer than {access.historical_recency_limit}; request ends too late"
                )

        max_page = ALPACA_CAPABILITIES.max_page_size
        page_size = request.page_size or max_page
        if max_page is not None and page_size is not None and page_size > max_page:
            raise UnsupportedCapabilityError(f"page size exceeds Alpaca's maximum of {max_page}")

        query = {
            "symbols": ",".join(self.normalize_symbol(s) for s in request.provider_symbols),
            "timeframe": self.provider_timeframe(request.timeframe),
            "start": _rfc3339(request.start),
            "end": _rfc3339(request.end),
            "feed": self.feed,
            "adjustment": "raw",
            "sort": "asc",
        }
        if page_size is not None:
            query["limit"] = str(page_size)
        if page_token:
            query["page_token"] = page_token
        return query

    def stream_url(self) -> str:
        feed = parse_feed(self.feed)
        self.feed_access().require_streaming()
        return f"{STREAM_BASE_URL}{_STREAM_PATHS[feed]}"

    def stream_channels(self) -> frozenset[DataType]:
        return ALPACA_CAPABILITIES.streaming_data_types

    # ------------------------------------------------------------ network operations (later)
    async def check_authentication(self) -> AuthenticationResult:
        raise ProviderOperationNotImplementedError(
            "Alpaca authentication check is implemented in Deliverable 2B"
        )

    def fetch_bars(self, request: HistoricalBarsRequest) -> AsyncIterator[BarPage]:
        raise ProviderOperationNotImplementedError(
            "Alpaca historical retrieval is implemented in Deliverable 2B"
        )

    def create_stream(self) -> MarketDataStream:
        raise ProviderOperationNotImplementedError(
            "Alpaca live streaming is implemented in Deliverable 2C"
        )

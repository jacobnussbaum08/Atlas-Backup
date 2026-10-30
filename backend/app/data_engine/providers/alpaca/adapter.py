"""Alpaca market-data adapter.

* 2A: identification, capabilities, plan entitlements, symbol normalization, offline
  configuration validation, request construction, stream URL selection.
* 2B: historical bar retrieval (``GET /v2/stocks/bars``) with complete pagination,
  pacing, retries, and an authentication check (``GET /v2/stocks/meta/exchanges``).
* 2C: live streaming over WebSocket (``stream.py``): bars, updated bars, daily bars,
  trades and quotes on the configured feed.

Only Alpaca's market-data hosts are used. Trading and brokerage endpoints are
never contacted. Nothing here returns placeholder or fabricated data.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from app.core.clocks import Clock, SystemClock
from app.core.config import MarketDataSettings, get_market_data_settings
from app.data_engine.capabilities import DataType, FeedAccess, FeedCoverage, ProviderCapabilities
from app.data_engine.enums import DataSource, StreamChannel
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    InvalidProviderSymbolError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    ProviderPaginationError,
    ProviderResponseError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.capabilities import (
    ADJUSTMENT_PARAMS,
    ALPACA_CAPABILITIES,
    DISPLAY_NAME,
    PROVIDER_ID,
    STREAMING_CHANNELS,
    feed_access,
    parse_feed,
)
from app.data_engine.providers.alpaca.config import AlpacaFeed, AlpacaSettings, get_alpaca_settings
from app.data_engine.providers.alpaca.http import AlpacaHttp, RequestPacer, Sleep
from app.data_engine.providers.alpaca.normalize import (
    BarFormatError,
    normalize_bar,
    normalize_quote,
    normalize_trade,
)
from app.data_engine.providers.alpaca.stream import AlpacaMarketDataStream
from app.data_engine.providers.base import (
    AuthenticationResult,
    BarPage,
    ConfigurationIssue,
    HistoricalBarsRequest,
    HistoricalObservationsRequest,
    MalformedRecord,
    MarketDataProvider,
    ObservationPage,
    ProviderConfigurationReport,
)
from app.data_engine.schemas import CredentialStatus, MarketDataEvent, Severity
from app.data_engine.timeframes import Timeframe

DATA_API_BASE_URL = "https://data.alpaca.markets"
STREAM_BASE_URL = "wss://stream.data.alpaca.markets"
HISTORICAL_BARS_PATH = "/v2/stocks/bars"
# Historical trades and quotes (2D recovery); responses are keyed by symbol like bars.
HISTORICAL_OBSERVATION_PATHS = {
    DataType.TRADE: "/v2/stocks/trades",
    DataType.QUOTE: "/v2/stocks/quotes",
}
_OBSERVATION_KEYS = {DataType.TRADE: "trades", DataType.QUOTE: "quotes"}
# Symbol-free market-data endpoint used to verify credentials.
AUTH_CHECK_PATH = "/v2/stocks/meta/exchanges"
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

    def __init__(
        self,
        settings: AlpacaSettings,
        clock: Clock | None = None,
        *,
        max_retries: int = 5,
        max_pages: int = 1000,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat_timeout: float = 30.0,
        stream_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._heartbeat_timeout = heartbeat_timeout
        # Tests point the stream at a local server; production always derives the URL
        # from the feed (there is deliberately no URL setting, see D-019).
        self._stream_url_override = stream_url
        self._clock = clock or SystemClock()
        self._max_pages = max_pages
        # Injectable so tests can use httpx.MockTransport; production uses the network.
        self._transport = transport
        access = feed_access(settings.data_feed, settings.data_plan)
        self._http = AlpacaHttp(
            clock=self._clock,
            pacer=RequestPacer(access.requests_per_minute, monotonic),
            max_retries=max_retries,
            sleep=sleep,
            scrub=tuple(
                secret.get_secret_value()
                for secret in (settings.api_key, settings.secret_key)
                if secret is not None
            ),
        )

    @classmethod
    def from_environment(cls, clock: Clock) -> AlpacaMarketDataProvider:
        market_data = get_market_data_settings()
        return cls(
            get_alpaca_settings(),
            clock,
            max_retries=market_data.historical_data_max_retries,
            max_pages=market_data.historical_data_max_pages,
            heartbeat_timeout=market_data.market_data_heartbeat_timeout,
        )

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
        never receive a different feed than the one it records. The adjustment is the
        request's explicit policy. ``asof`` is not sent, so Alpaca's default symbol
        mapping (renamed tickers resolved as of today) applies; see decision D-021.
        """
        access = self.feed_access()
        access.require_historical()
        ALPACA_CAPABILITIES.require_historical_bars(self.feed, request.timeframe)
        ALPACA_CAPABILITIES.require_adjustment(request.adjustment)
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
            "adjustment": ADJUSTMENT_PARAMS[request.adjustment],
            "sort": "asc",
        }
        if page_size is not None:
            query["limit"] = str(page_size)
        if page_token:
            query["page_token"] = page_token
        return query

    def validate_historical_request(self, request: HistoricalBarsRequest) -> None:
        self.historical_bars_query(request)

    def stream_url(self) -> str:
        feed = parse_feed(self.feed)
        self.feed_access().require_streaming()
        return f"{STREAM_BASE_URL}{_STREAM_PATHS[feed]}"

    def stream_channels(self) -> frozenset[DataType]:
        return ALPACA_CAPABILITIES.streaming_data_types

    def streaming_channels(self) -> frozenset[StreamChannel]:
        if not self.feed_access().streaming:
            return frozenset()
        return STREAMING_CHANNELS

    # ------------------------------------------------------------ network operations
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=DATA_API_BASE_URL,
            headers={**self.authentication_headers(), "Accept": "application/json"},
            timeout=self._settings.request_timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )

    async def check_authentication(self) -> AuthenticationResult:
        """Verify credentials with a symbol-free market-data request."""
        if self.credential_status() is CredentialStatus.NOT_CONFIGURED:
            return AuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.NOT_CONFIGURED,
                checked_at=self._clock.now(),
            )
        try:
            async with self._client() as client:
                await self._http.get(client, AUTH_CHECK_PATH, {})
        except ProviderAuthenticationError as exc:
            return AuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.REJECTED,
                checked_at=self._clock.now(),
                detail=exc.message,
            )
        except ProviderEntitlementError as exc:
            # Authenticated, but this resource is not part of the subscription.
            return AuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.VERIFIED,
                checked_at=self._clock.now(),
                detail=exc.message,
            )
        return AuthenticationResult(
            provider=self.provider_id,
            status=CredentialStatus.VERIFIED,
            checked_at=self._clock.now(),
        )

    async def fetch_bars(self, request: HistoricalBarsRequest) -> AsyncIterator[BarPage]:
        """Yield every page until Alpaca returns no ``next_page_token``.

        Raises ``ProviderPaginationError`` if a token repeats or the page limit is hit,
        so endless or truncated pagination never looks like a complete retrieval.
        """
        self.historical_bars_query(request)  # validate capability/entitlement before I/O
        async for (
            params,
            response,
            body,
            token,
            number,
            requested_at,
            received_at,
        ) in self._paginate(HISTORICAL_BARS_PATH, lambda t: self.historical_bars_query(request, t)):
            yield self._parse_page(
                request, params, response, body, token, number, requested_at, received_at
            )

    def historical_observations_query(
        self, request: HistoricalObservationsRequest, page_token: str | None = None
    ) -> dict[str, str]:
        """Query parameters for ``GET /v2/stocks/trades`` or ``/v2/stocks/quotes``.

        As for bars, the feed is always explicit and results are sorted ascending so that
        pages are contiguous in time.
        """
        access = self.feed_access()
        access.require_historical()
        if request.data_type not in ALPACA_CAPABILITIES.historical_data_types:
            raise UnsupportedCapabilityError(
                f"Alpaca has no historical {request.data_type.value.lower()}s"
            )
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
            "start": _rfc3339(request.start),
            "end": _rfc3339(request.end),
            "feed": self.feed,
            "sort": "asc",
        }
        if page_size is not None:
            query["limit"] = str(page_size)
        if page_token:
            query["page_token"] = page_token
        return query

    def validate_observations_request(self, request: HistoricalObservationsRequest) -> None:
        self.historical_observations_query(request)

    async def fetch_observations(
        self, request: HistoricalObservationsRequest
    ) -> AsyncIterator[ObservationPage]:
        self.historical_observations_query(request)  # validate before I/O
        path = HISTORICAL_OBSERVATION_PATHS[request.data_type]
        async for (
            params,
            response,
            body,
            token,
            number,
            requested_at,
            received_at,
        ) in self._paginate(path, lambda t: self.historical_observations_query(request, t)):
            yield self._parse_observations(
                request, params, response, body, token, number, requested_at, received_at
            )

    async def _paginate(
        self, path: str, query: Callable[[str | None], dict[str, str]]
    ) -> AsyncIterator[
        tuple[dict[str, str], httpx.Response, dict[str, Any], str | None, int, datetime, datetime]
    ]:
        """Follow ``next_page_token`` to the end; a repeated token or the page limit fails
        loudly, so endless or truncated pagination never looks complete."""
        seen_tokens: set[str] = set()
        token: str | None = None
        page_number = 0
        async with self._client() as client:
            while True:
                params = query(token)
                requested_at = self._clock.now()
                response = await self._http.get(client, path, params)
                received_at = self._clock.now()
                page_number += 1
                body = self._decode(response)
                next_token = self._next_token(body)
                yield params, response, body, next_token, page_number, requested_at, received_at
                if next_token is None:
                    return
                if next_token in seen_tokens or next_token == token:
                    raise ProviderPaginationError(
                        f"Alpaca returned a repeated page token after page {page_number}"
                    )
                if page_number >= self._max_pages:
                    raise ProviderPaginationError(
                        f"pagination exceeded the limit of {self._max_pages} pages"
                    )
                seen_tokens.add(next_token)
                token = next_token

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            body = json.loads(response.text, parse_float=Decimal)
        except ValueError:
            raise ProviderResponseError("Alpaca returned a non-JSON response") from None
        if not isinstance(body, dict):
            raise ProviderResponseError("Alpaca response is not a JSON object")
        return body

    @staticmethod
    def _next_token(body: dict[str, Any]) -> str | None:
        token = body.get("next_page_token")
        if token is None or token == "":
            return None
        if not isinstance(token, str):
            raise ProviderResponseError("next_page_token is not a string")
        return token

    def _parse_page(
        self,
        request: HistoricalBarsRequest,
        params: dict[str, str],
        response: httpx.Response,
        body: dict[str, Any],
        next_token: str | None,
        page_number: int,
        requested_at: datetime,
        received_at: datetime,
    ) -> BarPage:
        bars_by_symbol = body.get("bars")
        if bars_by_symbol is None:
            bars_by_symbol = {}  # documented as required; null is treated as "no bars"
        if not isinstance(bars_by_symbol, dict):
            raise ProviderResponseError("'bars' is not an object keyed by symbol")
        currency = body.get("currency")
        if currency is not None and not isinstance(currency, str):
            raise ProviderResponseError("'currency' is not a string")

        bars = []
        malformed = []
        for symbol, items in bars_by_symbol.items():
            if not isinstance(items, list):
                malformed.append(
                    MalformedRecord(
                        provider_symbol=str(symbol), reason="bars are not a list", item={}
                    )
                )
                continue
            for item in items:
                try:
                    bars.append(
                        normalize_bar(
                            item,
                            provider_symbol=str(symbol),
                            feed=self.feed,
                            timeframe=request.timeframe,
                            adjustment=request.adjustment,
                            received_at=received_at,
                        )
                    )
                except BarFormatError as exc:
                    malformed.append(
                        MalformedRecord(
                            provider_symbol=str(symbol),
                            reason=str(exc),
                            item=item if isinstance(item, dict) else {"value": repr(item)},
                        )
                    )

        raw_event = MarketDataEvent(
            provider=self.provider_id,
            feed=self.feed,
            source=DataSource.HISTORICAL_REST,
            message_type="bars_page",
            data_type=DataType.BAR,
            provider_symbol=",".join(request.provider_symbols),
            received_at=received_at,
            payload={},  # the verbatim body is in raw_body; parsed values are in ``bars``
            raw_body=response.text,
            request=params,
            http_status=response.status_code,
        )
        return BarPage(
            page_number=page_number,
            bars=tuple(bars),
            malformed=tuple(malformed),
            currency=currency,
            next_page_token=next_token,
            requested_at=requested_at,
            received_at=received_at,
            raw_event=raw_event,
        )

    def _parse_observations(
        self,
        request: HistoricalObservationsRequest,
        params: dict[str, str],
        response: httpx.Response,
        body: dict[str, Any],
        next_token: str | None,
        page_number: int,
        requested_at: datetime,
        received_at: datetime,
    ) -> ObservationPage:
        key = _OBSERVATION_KEYS[request.data_type]
        by_symbol = body.get(key)
        if by_symbol is None:
            by_symbol = {}  # no observations in the range
        if not isinstance(by_symbol, dict):
            raise ProviderResponseError(f"'{key}' is not an object keyed by symbol")
        normalize = normalize_trade if request.data_type is DataType.TRADE else normalize_quote
        parsed: list[Any] = []
        malformed = []
        for symbol, items in by_symbol.items():
            if not isinstance(items, list):
                malformed.append(
                    MalformedRecord(
                        provider_symbol=str(symbol), reason=f"{key} are not a list", item={}
                    )
                )
                continue
            for item in items:
                try:
                    if not isinstance(item, dict):
                        raise BarFormatError(f"{key[:-1]} is not an object")
                    parsed.append(
                        normalize(
                            {**item, "S": str(symbol)},
                            feed=self.feed,
                            received_at=received_at,
                            source=DataSource.HISTORICAL_REST,
                        )
                    )
                except BarFormatError as exc:
                    malformed.append(
                        MalformedRecord(
                            provider_symbol=str(symbol),
                            reason=str(exc),
                            item=item if isinstance(item, dict) else {"value": repr(item)},
                        )
                    )
        raw_event = MarketDataEvent(
            provider=self.provider_id,
            feed=self.feed,
            source=DataSource.HISTORICAL_REST,
            message_type=f"{key}_page",
            data_type=request.data_type,
            provider_symbol=",".join(request.provider_symbols),
            received_at=received_at,
            payload={},
            raw_body=response.text,
            request=params,
            http_status=response.status_code,
        )
        return ObservationPage(
            page_number=page_number,
            data_type=request.data_type,
            trades=tuple(parsed) if request.data_type is DataType.TRADE else (),
            quotes=tuple(parsed) if request.data_type is DataType.QUOTE else (),
            malformed=tuple(malformed),
            next_page_token=next_token,
            requested_at=requested_at,
            received_at=received_at,
            raw_event=raw_event,
        )

    def create_stream(self) -> AlpacaMarketDataStream:
        """A new, unconnected stream on the configured feed (one per connection attempt)."""
        key, secret = self._settings.api_key, self._settings.secret_key
        if key is None or secret is None:
            raise CredentialsNotConfiguredError("Alpaca credentials are not configured")
        url = self._stream_url_override or self.stream_url()  # checks the feed entitlement
        return AlpacaMarketDataStream(
            url=url,
            feed=self.feed,
            api_key=key,
            secret_key=secret,
            clock=self._clock,
            heartbeat_timeout=self._heartbeat_timeout,
        )

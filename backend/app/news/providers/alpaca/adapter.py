"""Alpaca news adapter (Phase 3C).

* Historical: ``GET https://data.alpaca.markets/v1beta1/news`` with ``start``/``end``
  (inclusive, RFC-3339), ``symbols`` (comma-separated; omitted = all news), ``limit``
  (max 50), ``sort``, ``include_content``, ``exclude_contentless`` and ``page_token``;
  the response is ``{"news": [...], "next_page_token": ...}``. Checked against
  https://docs.alpaca.markets/us/reference/news-3 on 2026-11-02.
* Real time: ``wss://stream.data.alpaca.markets/v1beta1/news`` (``stream.py``).

Authentication uses the existing market-data credentials (``ALPACA_API_KEY`` /
``ALPACA_SECRET_KEY``) in the documented headers; requests share the market-data HTTP
transport (pacing, retries, redacted errors). Only these two news endpoints are contacted.

Content: ``include_content`` is sent only when ``NEWS_STORE_CONTENT`` permits keeping
article bodies (D-074). Results are requested in ascending order so a long retrieval
progresses through time.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from app.core.clocks import Clock, SystemClock
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    InvalidProviderSymbolError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    ProviderPaginationError,
    ProviderResponseError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.capabilities import feed_access
from app.data_engine.providers.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.data_engine.providers.alpaca.http import AlpacaHttp, RequestPacer, Sleep
from app.data_engine.schemas import CredentialStatus
from app.news.config import NewsSettings, get_news_settings
from app.news.enums import ArticleOrigin
from app.news.providers.alpaca.normalize import PROVIDER_ID, NewsFormatError, normalize_article
from app.news.providers.alpaca.stream import AlpacaNewsStream
from app.news.providers.base import NewsProvider
from app.news.schemas import (
    MalformedArticle,
    NewsAuthenticationResult,
    NewsPage,
    NewsProviderCapabilities,
    NewsQuery,
    ProviderNewsArticle,
)

DATA_API_BASE_URL = "https://data.alpaca.markets"
NEWS_PATH = "/v1beta1/news"
NEWS_STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
KEY_HEADER = "APCA-API-KEY-ID"
SECRET_HEADER = "APCA-API-SECRET-KEY"  # noqa: S105  # header name, not a credential
MAX_PAGE_SIZE = 50

# Equities (AAPL, BRK.B) and crypto pairs (BTCUSD, BTC/USD) as Alpaca news tags them.
_SYMBOL = re.compile(r"^[A-Z0-9]{1,12}([./-][A-Z0-9]{1,8})?$")

CONTENT_POLICY = (
    "Alpaca relays articles from news partners (observed sources: benzinga, fiscal.ai). Its "
    "documentation states no retention or redistribution terms for article bodies, so Atlas "
    "keeps headline, summary, URL and metadata, and stores bodies only when "
    "NEWS_STORE_CONTENT=true is set by an operator who has confirmed the terms. Links point "
    "to the publisher; Atlas never fetches or scrapes the linked pages."
)

CAPABILITIES = NewsProviderCapabilities(
    provider=PROVIDER_ID,
    display_name="Alpaca News API",
    historical=True,
    streaming=True,
    max_page_size=MAX_PAGE_SIZE,
    symbol_filter=True,
    wildcard_stream=True,
    provider_categories=False,
    symbol_namespace="alpaca",
    content_policy=CONTENT_POLICY,
    notes=(
        "Articles carry the provider's related symbols, but no category, sentiment or "
        "importance metadata.",
        "Missing summaries and authors arrive as empty strings and are stored as missing.",
        "Request rate shares the Alpaca market-data plan limit (200/min on Basic); this "
        "adapter paces itself to that rate independently of the market-data adapter.",
        "The documentation does not say whether start/end filter on creation or update time; "
        "stream recovery uses an overlapping window and idempotent ingestion.",
        "One news stream connection per account (observed 2026-11-02: a second connection is "
        "refused with 406 'connection limit exceeded' during authentication).",
    ),
)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AlpacaNewsProvider(NewsProvider):
    provider_id: ClassVar[str] = PROVIDER_ID
    display_name: ClassVar[str] = "Alpaca News API"

    def __init__(
        self,
        settings: AlpacaSettings,
        news: NewsSettings,
        clock: Clock | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        stream_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._news = news
        self._clock = clock or SystemClock()
        self._transport = transport
        # Tests point the stream at a local fake; production always uses NEWS_STREAM_URL.
        self._stream_url = stream_url or NEWS_STREAM_URL
        access = feed_access(settings.data_feed, settings.data_plan)
        scrub = tuple(
            secret.get_secret_value()
            for secret in (settings.api_key, settings.secret_key)
            if secret is not None
        )
        self._http = AlpacaHttp(
            clock=self._clock,
            pacer=RequestPacer(access.requests_per_minute, monotonic),
            max_retries=news.historical_max_retries,
            sleep=sleep,
            scrub=scrub,
        )

    @classmethod
    def from_environment(cls, clock: Clock) -> AlpacaNewsProvider:
        return cls(get_alpaca_settings(), get_news_settings(), clock)

    def __repr__(self) -> str:
        return f"AlpacaNewsProvider(credentials={self.credential_status().value})"

    # ------------------------------------------------------------ identity
    def capabilities(self) -> NewsProviderCapabilities:
        return CAPABILITIES

    def credential_status(self) -> CredentialStatus:
        if self._settings.credentials_configured:
            return CredentialStatus.CONFIGURED
        return CredentialStatus.NOT_CONFIGURED

    def normalize_symbol(self, symbol: str) -> str:
        candidate = symbol.strip().upper()
        if not _SYMBOL.match(candidate):
            raise InvalidProviderSymbolError(f"'{symbol}' is not a valid Alpaca news symbol")
        return candidate

    def _headers(self) -> dict[str, str]:
        key, secret = self._settings.api_key, self._settings.secret_key
        if key is None or secret is None:
            raise CredentialsNotConfiguredError("Alpaca credentials are not configured")
        return {KEY_HEADER: key.get_secret_value(), SECRET_HEADER: secret.get_secret_value()}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=DATA_API_BASE_URL,
            headers={**self._headers(), "Accept": "application/json"},
            timeout=self._settings.request_timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )

    # ------------------------------------------------------------ requests
    def news_query(self, query: NewsQuery, page_token: str | None = None) -> dict[str, str]:
        if query.page_size > MAX_PAGE_SIZE:
            raise UnsupportedCapabilityError(
                f"page size exceeds Alpaca's maximum of {MAX_PAGE_SIZE}"
            )
        params = {
            "start": _rfc3339(query.start),
            "end": _rfc3339(query.end),
            "limit": str(query.page_size),
            "sort": "asc",
            "include_content": "true" if query.include_content else "false",
        }
        if query.symbols:
            params["symbols"] = ",".join(self.normalize_symbol(s) for s in query.symbols)
        if page_token:
            params["page_token"] = page_token
        return params

    async def check_authentication(self) -> NewsAuthenticationResult:
        if self.credential_status() is CredentialStatus.NOT_CONFIGURED:
            return NewsAuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.NOT_CONFIGURED.value,
                checked_at=self._clock.now(),
            )
        try:
            async with self._client() as client:
                await self._http.get(client, NEWS_PATH, {"limit": "1"})
        except ProviderAuthenticationError as exc:
            return NewsAuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.REJECTED.value,
                checked_at=self._clock.now(),
                detail=exc.message,
            )
        except ProviderEntitlementError as exc:
            return NewsAuthenticationResult(
                provider=self.provider_id,
                status=CredentialStatus.VERIFIED.value,
                checked_at=self._clock.now(),
                detail=f"authenticated, but news is not part of the subscription: {exc.message}",
            )
        return NewsAuthenticationResult(
            provider=self.provider_id,
            status=CredentialStatus.VERIFIED.value,
            checked_at=self._clock.now(),
        )

    async def fetch_news(self, query: NewsQuery) -> AsyncIterator[NewsPage]:
        """Every page until Alpaca returns no ``next_page_token``.

        Raises ``ProviderPaginationError`` if a token repeats or the page limit is reached
        (results so far have been yielded and stay valid).
        """
        include_content = query.include_content and self._news.store_content
        query = query.model_copy(update={"include_content": include_content})
        seen: set[str] = set()
        token: str | None = None
        page_number = 0
        async with self._client() as client:
            while True:
                page_number += 1
                if page_number > self._news.historical_max_pages:
                    raise ProviderPaginationError(
                        f"more than {self._news.historical_max_pages} pages; narrow the range"
                    )
                requested_at = self._clock.now()
                response = await self._http.get(client, NEWS_PATH, self.news_query(query, token))
                received_at = self._clock.now()
                page = self._page(response, page_number, requested_at, received_at)
                yield page
                token = page.next_page_token
                if not token:
                    return
                if token in seen:
                    raise ProviderPaginationError("Alpaca repeated a page token")
                seen.add(token)

    def _page(
        self, response: httpx.Response, number: int, requested_at: datetime, received_at: datetime
    ) -> NewsPage:
        try:
            body = response.json()
        except ValueError:
            raise ProviderResponseError("news response is not JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("news"), list):
            raise ProviderResponseError("news response has no 'news' list")
        token = body.get("next_page_token")
        if token is not None and not isinstance(token, str):
            raise ProviderResponseError("'next_page_token' is not a string")
        articles: list[ProviderNewsArticle] = []
        malformed: list[MalformedArticle] = []
        for item in body["news"]:
            if not isinstance(item, dict):
                malformed.append(
                    MalformedArticle(
                        provider_article_id=None,
                        reason="item is not an object",
                        item={"value": repr(item)[:200]},
                    )
                )
                continue
            try:
                articles.append(
                    normalize_article(
                        item,
                        origin=ArticleOrigin.HISTORICAL_REST,
                        received_at=received_at,
                        store_content=self._news.store_content,
                    )
                )
            except (NewsFormatError, ValueError) as exc:
                malformed.append(
                    MalformedArticle(
                        provider_article_id=str(item.get("id"))[:64],
                        reason=str(exc)[:300],
                        item=_without_content(item),
                    )
                )
        return NewsPage(
            page_number=number,
            articles=tuple(articles),
            malformed=tuple(malformed),
            next_page_token=token or None,
            requested_at=requested_at,
            received_at=received_at,
        )

    # ------------------------------------------------------------ streaming
    def create_stream(self) -> AlpacaNewsStream:
        key, secret = self._settings.api_key, self._settings.secret_key
        if key is None or secret is None:
            raise CredentialsNotConfiguredError("Alpaca credentials are not configured")
        return AlpacaNewsStream(
            url=self._stream_url,
            api_key=key,
            secret_key=secret,
            clock=self._clock,
            heartbeat_timeout=self._news.stream_heartbeat_timeout,
            store_content=self._news.store_content,
        )


def _without_content(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "content"}


__all__ = [
    "CAPABILITIES",
    "DATA_API_BASE_URL",
    "NEWS_PATH",
    "NEWS_STREAM_URL",
    "AlpacaNewsProvider",
]

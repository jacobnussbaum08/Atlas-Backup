"""Alpaca news REST adapter against a FAKE server (httpx.MockTransport) (Phase 3C).

These verify request construction, authentication headers, pagination and error handling
with constructed fixtures. They do NOT prove connectivity to Alpaca (see tests/live).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    InvalidProviderSymbolError,
    ProviderAuthenticationError,
    ProviderPaginationError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.data_engine.schemas import CredentialStatus
from app.news.enums import ArticleOrigin
from app.news.schemas import NewsQuery
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, FakeAlpaca
from app.tests.fixtures.alpaca_news import fake_news_provider, news_item, news_page, pages

NOW = datetime(2026, 3, 4, 18, 0, tzinfo=UTC)
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _query(**overrides) -> NewsQuery:
    values = {"start": T0, "end": T0 + timedelta(hours=6), "page_size": 2} | overrides
    return NewsQuery(**values)


def test_request_uses_documented_endpoint_headers_and_parameters():
    fake = FakeAlpaca().add(news_page([news_item(1, T0)]))
    result = pages(fake_news_provider(fake, now=NOW), _query(symbols=("AAPL", "MSFT")))

    request = fake.requests[0]
    assert request.url.host == "data.alpaca.markets"
    assert request.url.path == "/v1beta1/news"
    assert request.headers["APCA-API-KEY-ID"] == FAKE_KEY
    assert request.headers["APCA-API-SECRET-KEY"] == FAKE_SECRET
    assert fake.params(0) == {
        "start": "2026-03-02T14:30:00Z",
        "end": "2026-03-02T20:30:00Z",
        "limit": "2",
        "sort": "asc",
        "include_content": "false",
        "symbols": "AAPL,MSFT",
    }
    [page] = result
    assert page.page_number == 1
    assert page.next_page_token is None
    assert page.articles[0].origin is ArticleOrigin.HISTORICAL_REST
    assert page.articles[0].received_at == NOW


def test_all_news_omits_symbols_and_content_needs_permission():
    fake = FakeAlpaca().add(news_page([])).add(news_page([]))
    pages(fake_news_provider(fake, now=NOW), _query(include_content=True))
    assert "symbols" not in fake.params(0)
    assert fake.params(0)["include_content"] == "false"  # retention not permitted
    pages(fake_news_provider(fake, now=NOW, store_content=True), _query(include_content=True))
    assert fake.params(1)["include_content"] == "true"


def test_pagination_follows_tokens_until_none():
    fake = (
        FakeAlpaca()
        .add(news_page([news_item(1, T0), news_item(2, T0)], "tok-1"))
        .add(news_page([news_item(3, T0)], "tok-2"))
        .add(news_page([news_item(4, T0)], None))
    )
    result = pages(fake_news_provider(fake, now=NOW), _query())
    assert [p.page_number for p in result] == [1, 2, 3]
    assert [a.provider_article_id for p in result for a in p.articles] == ["1", "2", "3", "4"]
    assert "page_token" not in fake.params(0)
    assert fake.params(1)["page_token"] == "tok-1"
    assert fake.params(2)["page_token"] == "tok-2"


def test_repeated_token_and_page_limit_fail_loudly():
    fake = (
        FakeAlpaca()
        .add(news_page([news_item(1, T0)], "same"))
        .add(news_page([news_item(2, T0)], "same"))
    )
    with pytest.raises(ProviderPaginationError, match="repeated"):
        pages(fake_news_provider(fake, now=NOW), _query())
    fake = FakeAlpaca().add(news_page([], "a")).add(news_page([], "b"))
    with pytest.raises(ProviderPaginationError, match="more than 1 pages"):
        pages(fake_news_provider(fake, now=NOW, historical_max_pages=1), _query())


def test_malformed_items_are_reported_not_dropped():
    fake = FakeAlpaca().add(
        news_page([news_item(1, T0), {**news_item(2, T0), "headline": ""}, "junk"])
    )
    [page] = pages(fake_news_provider(fake, now=NOW), _query())
    assert [a.provider_article_id for a in page.articles] == ["1"]
    assert [m.reason for m in page.malformed] == [
        "article has no headline",
        "item is not an object",
    ]


def test_invalid_response_shape_is_an_error():
    fake = FakeAlpaca().add(httpx.Response(200, json={"articles": []}))
    with pytest.raises(ProviderResponseError):
        pages(fake_news_provider(fake, now=NOW), _query())


def test_rejected_credentials_are_permanent_and_never_echoed():
    fake = FakeAlpaca().add_error(401, f"bad key {FAKE_KEY}")
    with pytest.raises(ProviderAuthenticationError) as caught:
        pages(fake_news_provider(fake, now=NOW), _query())
    assert FAKE_KEY not in str(caught.value)
    assert len(fake.requests) == 1  # not retried


def test_outages_are_retried_then_reported():
    fake = (
        FakeAlpaca()
        .add_error(503, "down")
        .add(httpx.Response(429, json={"message": "slow down"}))
        .add(news_page([news_item(1, T0)]))
    )
    [page] = pages(fake_news_provider(fake, now=NOW), _query())
    assert len(page.articles) == 1
    assert len(fake.requests) == 3

    always_down = FakeAlpaca()
    for _ in range(3):
        always_down.add_error(500, "boom")
    with pytest.raises(ProviderUnavailableError):
        pages(fake_news_provider(always_down, now=NOW, historical_max_retries=2), _query())

    bad_request = FakeAlpaca().add_error(400, "invalid symbols")
    with pytest.raises(ProviderRequestError):
        pages(fake_news_provider(bad_request, now=NOW), _query())


def test_check_authentication_outcomes():
    ok = FakeAlpaca().add(news_page([]))
    result = asyncio.run(fake_news_provider(ok, now=NOW).check_authentication())
    assert result.status == CredentialStatus.VERIFIED.value
    assert ok.params(0) == {"limit": "1"}

    rejected = FakeAlpaca().add_error(401, "forbidden")
    result = asyncio.run(fake_news_provider(rejected, now=NOW).check_authentication())
    assert result.status == CredentialStatus.REJECTED.value

    entitlement = FakeAlpaca().add_error(403, "not in plan")
    result = asyncio.run(fake_news_provider(entitlement, now=NOW).check_authentication())
    assert result.status == CredentialStatus.VERIFIED.value
    assert "not part of the subscription" in (result.detail or "")

    none = fake_news_provider(FakeAlpaca(), now=NOW, credentials=False)
    assert none.credential_status() is CredentialStatus.NOT_CONFIGURED
    result = asyncio.run(none.check_authentication())
    assert result.status == CredentialStatus.NOT_CONFIGURED.value
    with pytest.raises(CredentialsNotConfiguredError):
        none.create_stream()


def test_symbols_are_validated_and_repr_hides_credentials():
    provider = fake_news_provider(FakeAlpaca(), now=NOW)
    assert provider.normalize_symbol(" brk.b ") == "BRK.B"
    assert provider.normalize_symbol("BTC/USD") == "BTC/USD"
    with pytest.raises(InvalidProviderSymbolError):
        provider.normalize_symbol("not a symbol")
    assert FAKE_KEY not in repr(provider)
    assert FAKE_SECRET not in repr(provider)
    caps = provider.capabilities()
    assert caps.historical
    assert caps.streaming
    assert caps.max_page_size == 50
    assert caps.provider_categories is False
    assert caps.symbol_namespace == "alpaca"
    assert "NEWS_STORE_CONTENT" in caps.content_policy


def test_page_size_above_the_documented_maximum_is_refused():
    from app.data_engine.errors import UnsupportedCapabilityError

    provider = fake_news_provider(FakeAlpaca(), now=NOW)
    with pytest.raises(UnsupportedCapabilityError):
        provider.news_query(_query(page_size=51))

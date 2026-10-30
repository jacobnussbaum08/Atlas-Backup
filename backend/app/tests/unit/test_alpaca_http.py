"""Alpaca REST retrieval against a FAKE server (httpx.MockTransport).

These tests verify Atlas's request construction, pagination, parsing and error
handling with constructed fixtures. They do NOT prove connectivity to Alpaca; see
tests/live for the opt-in real-provider smoke test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.data_engine.enums import PriceAdjustment
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    ProviderPaginationError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.data_engine.providers.base import HistoricalBarsRequest
from app.data_engine.schemas import CredentialStatus
from app.tests.fixtures.alpaca import (
    FAKE_KEY,
    FAKE_SECRET,
    FakeAlpaca,
    RecordingSleep,
    fake_provider,
    fixture_bar,
)

NOW = datetime(2026, 3, 4, 18, 0, tzinfo=UTC)
DAY1 = datetime(2026, 3, 2, 5, 0, tzinfo=UTC)  # midnight New York (EST)
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _request(**overrides) -> HistoricalBarsRequest:
    values = {
        "provider_symbols": ("AAPL",),
        "timeframe": "1Min",
        "start": T0,
        "end": T0 + timedelta(minutes=10),
        "adjustment": PriceAdjustment.RAW,
        "page_size": 2,
    } | overrides
    return HistoricalBarsRequest(**values)


async def _collect(provider, request):
    return [page async for page in provider.fetch_bars(request)]


def _pages(provider, request=None):
    return asyncio.run(_collect(provider, request or _request()))


# ------------------------------------------------------------------ request construction
def test_request_uses_documented_endpoint_headers_and_explicit_parameters():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0)]})
    _pages(fake_provider(fake, now=NOW))

    request = fake.requests[0]
    assert request.url.host == "data.alpaca.markets"
    assert request.url.path == "/v2/stocks/bars"
    assert request.headers["APCA-API-KEY-ID"] == FAKE_KEY
    assert request.headers["APCA-API-SECRET-KEY"] == FAKE_SECRET
    assert fake.params(0) == {
        "symbols": "AAPL",
        "timeframe": "1Min",
        "start": "2026-03-02T14:30:00Z",
        "end": "2026-03-02T14:40:00Z",
        "feed": "iex",
        "adjustment": "raw",
        "sort": "asc",
        "limit": "2",
    }


def test_missing_credentials_fail_before_any_request():
    fake = FakeAlpaca()
    provider = fake_provider(fake, now=NOW, api_key=None, secret_key=None)
    with pytest.raises(CredentialsNotConfiguredError):
        _pages(provider)
    assert fake.requests == []


# ------------------------------------------------------------------ pagination
def test_follows_next_page_token_until_exhausted():
    fake = (
        FakeAlpaca()
        .add_page({"AAPL": [fixture_bar(T0), fixture_bar(T0 + timedelta(minutes=1))]}, "tok-1")
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=2))]}, "tok-2")
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=3))]}, None)
    )
    pages = _pages(fake_provider(fake, now=NOW))

    assert [p.page_number for p in pages] == [1, 2, 3]
    assert [len(p.bars) for p in pages] == [2, 1, 1]
    assert "page_token" not in fake.params(0)
    assert fake.params(1)["page_token"] == "tok-1"
    assert fake.params(2)["page_token"] == "tok-2"
    assert pages[-1].next_page_token is None


def test_empty_token_string_ends_pagination():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0)]}, "")
    assert len(_pages(fake_provider(fake, now=NOW))) == 1


def test_repeated_page_token_is_detected():
    fake = (
        FakeAlpaca()
        .add_page({"AAPL": [fixture_bar(T0)]}, "loop")
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=1))]}, "loop")
    )
    with pytest.raises(ProviderPaginationError, match="repeated page token"):
        _pages(fake_provider(fake, now=NOW))


def test_page_limit_stops_runaway_pagination():
    fake = FakeAlpaca()
    for i in range(3):
        fake.add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=i))]}, f"tok-{i}")
    with pytest.raises(ProviderPaginationError, match="limit of 2 pages"):
        _pages(fake_provider(fake, now=NOW, max_pages=2))


def test_multiple_symbols_are_kept_apart():
    fake = FakeAlpaca().add_page(
        {
            "AAPL": [fixture_bar(T0)],
            "SPY": [fixture_bar(T0), fixture_bar(T0 + timedelta(minutes=1))],
        }
    )
    (page,) = _pages(fake_provider(fake, now=NOW), _request(provider_symbols=("AAPL", "SPY")))

    assert fake.params(0)["symbols"] == "AAPL,SPY"
    assert sorted(b.provider_symbol for b in page.bars) == ["AAPL", "SPY", "SPY"]


@pytest.mark.parametrize("bars", [{}, None], ids=["empty-object", "null"])
def test_empty_responses_yield_an_empty_page(bars):
    fake = FakeAlpaca().add_page(bars)
    (page,) = _pages(fake_provider(fake, now=NOW))
    assert page.bars == ()
    assert page.malformed == ()


# ------------------------------------------------------------------ normalization
def test_numbers_are_parsed_exactly_as_decimals():
    fake = FakeAlpaca().add_page(
        {
            "AAPL": [
                fixture_bar(
                    T0, o="187.1234", h="187.9999", low="187.0001", c="187.5", vw="187.33333333"
                )
            ]
        }
    )
    (page,) = _pages(fake_provider(fake, now=NOW))
    bar = page.bars[0]

    assert bar.open == Decimal("187.1234")
    assert bar.high == Decimal("187.9999")
    assert bar.vwap == Decimal("187.33333333")
    assert isinstance(bar.close, Decimal)
    assert bar.volume == Decimal(1200)
    assert bar.trade_count == 15


def test_bar_provenance_and_timestamps():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0)]})
    (page,) = _pages(fake_provider(fake, now=NOW))
    bar = page.bars[0]

    assert bar.event_time == T0
    assert bar.event_time.tzinfo is UTC
    assert bar.feed == "iex"
    assert bar.adjustment is PriceAdjustment.RAW
    assert bar.source.value == "HISTORICAL_REST"
    assert bar.received_at == NOW
    assert bar.available_at == NOW
    assert bar.original_available_at is None  # never claimed for backfill
    assert bar.is_complete is True
    assert page.raw_event.raw_body is not None
    assert page.raw_event.request["feed"] == "iex"
    assert "APCA" not in str(page.raw_event.request)


def test_missing_optional_fields_stay_none_not_zero():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0, v=None, n=None, vw=None)]})
    (page,) = _pages(fake_provider(fake, now=NOW))
    bar = page.bars[0]
    assert bar.volume is None
    assert bar.trade_count is None
    assert bar.vwap is None


def test_zero_volume_stays_zero():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0, v=0)]})
    (page,) = _pages(fake_provider(fake, now=NOW))
    assert page.bars[0].volume == Decimal(0)


def test_unfinished_bars_are_marked_incomplete():
    now = T0 + timedelta(seconds=30)  # the 14:30 minute bar is still forming
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(T0)]})
    (page,) = _pages(fake_provider(fake, now=now), _request(end=T0 + timedelta(minutes=1)))
    assert page.bars[0].is_complete is False


def test_daily_bar_completion_uses_new_york_midnight():
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(DAY1)]})
    (page,) = _pages(
        fake_provider(fake, now=NOW),
        _request(timeframe="1Day", start=DAY1, end=DAY1 + timedelta(days=1)),
    )
    assert page.bars[0].is_complete is True


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ({"t": "2026-03-02T14:30:00Z", "h": 1, "l": 1, "c": 1}, "missing required field 'o'"),
        ({"t": "not-a-time", "o": 1, "h": 1, "l": 1, "c": 1}, "invalid RFC-3339"),
        ({"t": "2026-03-02T14:30:00", "o": 1, "h": 1, "l": 1, "c": 1}, "invalid RFC-3339"),
        (
            {"t": "2026-03-02T14:30:00.000000001Z", "o": 1, "h": 1, "l": 1, "c": 1},
            "sub-microsecond",
        ),
        ({"t": "2026-03-02T14:30:00Z", "o": "1", "h": 1, "l": 1, "c": 1}, "not a number"),
        ({"t": "2026-03-02T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "n": 1.5}, "not an integer"),
    ],
)
def test_malformed_bars_are_reported_not_dropped(item, reason):
    fake = FakeAlpaca().add(
        httpx.Response(200, json={"bars": {"AAPL": [item]}, "next_page_token": None})
    )
    (page,) = _pages(fake_provider(fake, now=NOW))
    assert page.bars == ()
    assert len(page.malformed) == 1
    assert reason in page.malformed[0].reason


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"bars": [], "next_page_token": null}',
        '{"bars": {}, "next_page_token": 5}',
    ],
)
def test_malformed_responses_raise(body):
    fake = FakeAlpaca().add(httpx.Response(200, text=body))
    with pytest.raises(ProviderResponseError):
        _pages(fake_provider(fake, now=NOW))


# ------------------------------------------------------------------ errors and retries
def test_unauthorized_is_permanent_and_never_retried():
    fake = FakeAlpaca().add_error(401, "forbidden")
    with pytest.raises(ProviderAuthenticationError):
        _pages(fake_provider(fake, now=NOW))
    assert len(fake.requests) == 1


def test_subscription_refusal_is_an_entitlement_error():
    fake = FakeAlpaca().add_error(403, "subscription does not permit querying recent SIP data")
    with pytest.raises(ProviderEntitlementError, match="recent SIP data"):
        _pages(fake_provider(fake, now=NOW))
    assert len(fake.requests) == 1


def test_invalid_request_is_not_retried_and_message_is_redacted():
    fake = FakeAlpaca().add_error(
        400, f"invalid timeframe (echo: {FAKE_KEY} / {FAKE_SECRET}; token=SHOULDNOTLEAK)"
    )
    with pytest.raises(ProviderRequestError) as excinfo:
        _pages(fake_provider(fake, now=NOW))
    message = excinfo.value.message
    assert "invalid timeframe" in message
    assert FAKE_KEY not in message
    assert FAKE_SECRET not in message
    assert "SHOULDNOTLEAK" not in message
    assert len(fake.requests) == 1


def test_server_errors_retry_with_bounded_backoff_then_succeed():
    sleep = RecordingSleep()
    fake = FakeAlpaca().add_error(500).add_error(503).add_page({"AAPL": [fixture_bar(T0)]})
    pages = _pages(fake_provider(fake, now=NOW, sleep=sleep))

    assert len(pages[0].bars) == 1
    assert len(fake.requests) == 3
    assert 0.5 in sleep.delays
    assert 1.0 in sleep.delays


def test_retries_are_bounded():
    sleep = RecordingSleep()
    fake = FakeAlpaca()
    for _ in range(3):
        fake.add_error(502)
    with pytest.raises(ProviderUnavailableError, match="server error 502"):
        _pages(fake_provider(fake, now=NOW, sleep=sleep, max_retries=2))
    assert len(fake.requests) == 3


def test_timeouts_and_network_errors_are_retried():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    fake = FakeAlpaca().add(timeout).add(refused).add_page({"AAPL": [fixture_bar(T0)]})
    pages = _pages(fake_provider(fake, now=NOW))
    assert len(pages) == 1
    assert len(fake.requests) == 3


def test_rate_limit_waits_until_reset_then_retries():
    sleep = RecordingSleep()
    reset = str(int(NOW.timestamp()) + 7)
    fake = (
        FakeAlpaca()
        .add_error(429, "too many requests", headers={"X-RateLimit-Reset": reset})
        .add_page({"AAPL": [fixture_bar(T0)]})
    )
    _pages(fake_provider(fake, now=NOW, sleep=sleep))
    assert 7.0 in sleep.delays


def test_rate_limit_exhaustion_raises_after_retries():
    fake = FakeAlpaca()
    for _ in range(2):
        fake.add_error(429)
    with pytest.raises(ProviderRateLimitError):
        _pages(fake_provider(fake, now=NOW, max_retries=1))


def test_requests_are_paced_to_the_plan_rate():
    """Basic plan: 200 requests/minute -> one request every 0.3 s."""
    sleep = RecordingSleep()
    fake = (
        FakeAlpaca()
        .add_page({"AAPL": [fixture_bar(T0)]}, "a")
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=1))]}, "b")
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=2))]})
    )
    _pages(fake_provider(fake, now=NOW, sleep=sleep))
    assert sleep.delays == pytest.approx([0.3, 0.3])


def test_exhausted_quota_pauses_until_reset():
    sleep = RecordingSleep()
    reset = str(int(NOW.timestamp()) + 20)
    fake = (
        FakeAlpaca()
        .add_page(
            {"AAPL": [fixture_bar(T0)]},
            "a",
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset},
        )
        .add_page({"AAPL": [fixture_bar(T0 + timedelta(minutes=1))]})
    )
    _pages(fake_provider(fake, now=NOW, sleep=sleep))
    assert sleep.delays == pytest.approx([20.0])


# ------------------------------------------------------------------ authentication check
def test_authentication_check_uses_symbol_free_metadata_endpoint():
    fake = FakeAlpaca().add(httpx.Response(200, json={"V": "IEX"}))
    result = asyncio.run(fake_provider(fake, now=NOW).check_authentication())

    assert result.status is CredentialStatus.VERIFIED
    assert fake.requests[0].url.path == "/v2/stocks/meta/exchanges"
    assert fake.requests[0].url.host == "data.alpaca.markets"


def test_authentication_check_reports_rejection_without_secrets():
    fake = FakeAlpaca().add_error(401, "access key verification failed")
    result = asyncio.run(fake_provider(fake, now=NOW).check_authentication())

    assert result.status is CredentialStatus.REJECTED
    assert FAKE_KEY not in (result.detail or "")
    assert FAKE_SECRET not in (result.detail or "")


def test_authentication_check_without_credentials_makes_no_request():
    fake = FakeAlpaca()
    result = asyncio.run(
        fake_provider(fake, now=NOW, api_key=None, secret_key=None).check_authentication()
    )
    assert result.status is CredentialStatus.NOT_CONFIGURED
    assert fake.requests == []

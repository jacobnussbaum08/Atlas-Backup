"""Alpaca adapter foundation (no network). Credentials are fake fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    InvalidProviderSymbolError,
    ProviderConfigurationError,
    ProviderOperationNotImplementedError,
    UnknownProviderError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.adapter import (
    KEY_HEADER,
    SECRET_HEADER,
    AlpacaMarketDataProvider,
)
from app.data_engine.providers.alpaca.config import AlpacaSettings
from app.data_engine.providers.base import HistoricalBarsRequest, MarketDataProvider
from app.data_engine.providers.registry import available_providers, create_provider
from app.data_engine.schemas import CredentialStatus, Severity

FAKE_KEY = "TESTKEYFIXTURE0001"
FAKE_SECRET = "TestSecretFixtureNotReal0001"
NOW = datetime(2026, 3, 2, 18, 0, tzinfo=UTC)


def _provider(**settings) -> AlpacaMarketDataProvider:
    values = {"api_key": FAKE_KEY, "secret_key": FAKE_SECRET} | settings
    return AlpacaMarketDataProvider(AlpacaSettings(_env_file=None, **values), FixedClock(NOW))


def _md(**values) -> MarketDataSettings:
    return MarketDataSettings(_env_file=None, **values)


def _request(**overrides) -> HistoricalBarsRequest:
    values = {
        "provider_symbols": ("SPY", "AAPL"),
        "timeframe": "5Min",
        "start": NOW - timedelta(days=2),
        "end": NOW - timedelta(days=1),
    } | overrides
    return HistoricalBarsRequest(**values)


def _codes(report) -> dict[str, Severity]:
    return {issue.code: issue.severity for issue in report.issues}


# ------------------------------------------------------------------ identity and registry
def test_implements_the_provider_contract():
    provider = _provider()
    assert isinstance(provider, MarketDataProvider)
    assert provider.provider_id == "alpaca"
    assert provider.feed == "iex"
    assert provider.capabilities().provider == "alpaca"


def test_registry_builds_the_configured_provider(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert available_providers() == ("alpaca",)
    assert isinstance(create_provider(_md()), AlpacaMarketDataProvider)
    with pytest.raises(UnknownProviderError, match="unknown market-data provider 'polygon'"):
        create_provider(_md(market_data_provider="polygon"))


def test_repr_never_contains_credentials():
    text = repr(_provider())
    assert FAKE_KEY not in text
    assert FAKE_SECRET not in text
    assert "CONFIGURED" in text


# ------------------------------------------------------------------ symbols
@pytest.mark.parametrize(("raw", "expected"), [("spy", "SPY"), (" BRK.B ", "BRK.B")])
def test_symbol_normalisation(raw, expected):
    assert _provider().normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "EUR/USD", "BTC-USD", "TOO.MANY.DOTS", "ABCDEFGHIJK"])
def test_invalid_symbols_are_rejected(raw):
    with pytest.raises(InvalidProviderSymbolError):
        _provider().normalize_symbol(raw)


# ------------------------------------------------------------------ credentials
def test_credential_status_is_offline_only():
    assert _provider().credential_status() is CredentialStatus.CONFIGURED
    assert (
        _provider(api_key=None, secret_key=None).credential_status()
        is CredentialStatus.NOT_CONFIGURED
    )


def test_authentication_headers_use_documented_names():
    headers = _provider().authentication_headers()
    assert headers == {KEY_HEADER: FAKE_KEY, SECRET_HEADER: FAKE_SECRET}
    assert KEY_HEADER == "APCA-API-KEY-ID"
    assert SECRET_HEADER == "APCA-API-SECRET-KEY"


def test_authentication_headers_require_credentials():
    with pytest.raises(CredentialsNotConfiguredError):
        _provider(api_key=None, secret_key=None).authentication_headers()


# ------------------------------------------------------------------ configuration report
def test_default_configuration_is_valid_with_warnings():
    report = _provider().validate_configuration(_md(market_data_symbols="SPY,QQQ"))
    codes = _codes(report)

    assert report.ok
    assert codes["single_venue_feed"] is Severity.WARNING  # IEX is never presented as the market
    assert codes["stream_disabled"] is Severity.INFO
    assert "credentials_not_configured" not in codes


def test_missing_credentials_warn_when_idle_but_block_streaming():
    provider = _provider(api_key=None, secret_key=None)

    idle = provider.validate_configuration(_md(market_data_symbols="SPY"))
    streaming = provider.validate_configuration(
        _md(market_data_symbols="SPY", market_data_stream_enabled=True)
    )

    assert idle.ok
    assert _codes(idle)["credentials_not_configured"] is Severity.WARNING
    assert not streaming.ok
    with pytest.raises(ProviderConfigurationError, match="credentials_not_configured"):
        streaming.raise_for_errors()


def test_streaming_sip_on_basic_plan_is_an_error_not_a_silent_downgrade():
    report = _provider(data_feed="sip").validate_configuration(
        _md(market_data_symbols="SPY", market_data_stream_enabled=True)
    )
    assert _codes(report)["feed_not_streamable"] is Severity.ERROR
    assert report.feed == "sip"


def test_stream_symbol_limit_and_empty_universe():
    symbols = ",".join(f"S{i:03d}" for i in range(31))
    too_many = _provider().validate_configuration(
        _md(market_data_symbols=symbols, market_data_stream_enabled=True)
    )
    empty = _provider().validate_configuration(_md(market_data_stream_enabled=True))

    assert _codes(too_many)["stream_symbol_limit"] is Severity.ERROR
    assert _codes(empty)["no_symbols"] is Severity.ERROR


def test_invalid_symbols_and_oversized_batches_are_errors():
    report = _provider().validate_configuration(
        _md(market_data_symbols="SPY,BTC/USD", historical_data_batch_size=20_000)
    )
    codes = _codes(report)
    assert codes["invalid_symbols"] is Severity.ERROR
    assert codes["batch_size_too_large"] is Severity.ERROR


def test_report_never_contains_credentials():
    report = _provider().validate_configuration(_md(market_data_symbols="SPY"))
    dumped = report.model_dump_json()
    assert FAKE_KEY not in dumped
    assert FAKE_SECRET not in dumped


# ------------------------------------------------------------------ historical request construction
def test_historical_query_always_names_the_feed_explicitly():
    query = _provider().historical_bars_query(_request(page_size=500))

    assert query == {
        "symbols": "SPY,AAPL",
        "timeframe": "5Min",
        "start": "2026-02-28T18:00:00Z",
        "end": "2026-03-01T18:00:00Z",
        "feed": "iex",
        "adjustment": "raw",
        "sort": "asc",
        "limit": "500",
    }


def test_historical_query_defaults_to_maximum_page_and_passes_page_token():
    query = _provider().historical_bars_query(_request(), page_token="opaque-token")
    assert query["limit"] == "10000"
    assert query["page_token"] == "opaque-token"


def test_historical_query_rejects_unsupported_requests():
    provider = _provider()
    with pytest.raises(UnsupportedCapabilityError, match="2Day"):
        provider.historical_bars_query(_request(timeframe="2Day"))
    with pytest.raises(UnsupportedCapabilityError, match="maximum"):
        provider.historical_bars_query(_request(page_size=10_001))
    with pytest.raises(InvalidProviderSymbolError):
        provider.historical_bars_query(_request(provider_symbols=("EUR/USD",)))


def test_basic_plan_sip_history_refuses_the_latest_15_minutes():
    provider = _provider(data_feed="sip")
    provider.historical_bars_query(_request(end=NOW - timedelta(minutes=16)))
    with pytest.raises(UnsupportedCapabilityError, match="newer than"):
        provider.historical_bars_query(_request(end=NOW - timedelta(minutes=5)))


def test_delayed_sip_has_no_history():
    with pytest.raises(UnsupportedCapabilityError, match="historical"):
        _provider(data_feed="delayed_sip").historical_bars_query(_request())


# ------------------------------------------------------------------ streaming endpoint selection
def test_stream_url_matches_feed_and_entitlement():
    assert _provider().stream_url() == "wss://stream.data.alpaca.markets/v2/iex"
    assert (
        _provider(data_feed="sip", data_plan="algo_trader_plus").stream_url()
        == "wss://stream.data.alpaca.markets/v2/sip"
    )
    with pytest.raises(UnsupportedCapabilityError):
        _provider(data_feed="sip").stream_url()  # Basic plan: no real-time SIP


def test_only_market_data_hosts_are_used():
    from app.data_engine.providers.alpaca import adapter

    hosts = {adapter.DATA_API_BASE_URL, adapter.STREAM_BASE_URL}
    assert hosts == {"https://data.alpaca.markets", "wss://stream.data.alpaca.markets"}
    assert not any("api.alpaca.markets" in h or "paper-api" in h for h in hosts)


# ------------------------------------------------------------------ later deliverables
async def _call(coro_or_iter):
    return await coro_or_iter


def test_network_operations_are_explicitly_not_implemented_yet():
    import asyncio

    provider = _provider()
    with pytest.raises(ProviderOperationNotImplementedError, match="2B"):
        provider.fetch_bars(_request())
    with pytest.raises(ProviderOperationNotImplementedError, match="2C"):
        provider.create_stream()
    with pytest.raises(ProviderOperationNotImplementedError, match="2B"):
        asyncio.run(_call(provider.check_authentication()))

from __future__ import annotations

from datetime import timedelta

import pytest

from app.data_engine.capabilities import DataType, FeedCoverage
from app.data_engine.errors import UnsupportedCapabilityError
from app.data_engine.providers.alpaca.capabilities import (
    ALPACA_CAPABILITIES,
    feed_access,
    parse_feed,
)
from app.data_engine.providers.alpaca.config import AlpacaDataPlan, AlpacaFeed
from app.data_engine.timeframes import INITIAL_TIMEFRAMES, Timeframe
from app.database.models.asset import AssetClass

BASIC = AlpacaDataPlan.BASIC
PLUS = AlpacaDataPlan.ALGO_TRADER_PLUS


def test_iex_is_single_venue_and_sip_consolidated():
    assert ALPACA_CAPABILITIES.feed("iex").coverage is FeedCoverage.SINGLE_VENUE
    assert ALPACA_CAPABILITIES.feed("sip").coverage is FeedCoverage.CONSOLIDATED
    assert ALPACA_CAPABILITIES.feed("delayed_sip").delay == timedelta(minutes=15)


def test_scope_is_us_equities_and_etfs_only():
    assert ALPACA_CAPABILITIES.asset_classes == {AssetClass.EQUITY, AssetClass.ETF}


def test_unknown_feed_is_an_explicit_error():
    with pytest.raises(UnsupportedCapabilityError, match="no feed 'boats'"):
        ALPACA_CAPABILITIES.feed("boats")
    with pytest.raises(UnsupportedCapabilityError, match="not supported by Atlas"):
        parse_feed("overnight")


@pytest.mark.parametrize("timeframe", INITIAL_TIMEFRAMES, ids=str)
def test_all_initial_timeframes_are_available_historically(timeframe):
    assert ALPACA_CAPABILITIES.supports_historical_timeframe(timeframe)


@pytest.mark.parametrize("text", ["60Min", "24Hour", "2Day", "2Week", "5Month"])
def test_out_of_range_timeframes_are_not_supported(text):
    assert not ALPACA_CAPABILITIES.supports_historical_timeframe(Timeframe.parse(text))


def test_require_historical_bars_checks_feed_and_timeframe():
    ALPACA_CAPABILITIES.require_historical_bars("iex", Timeframe.parse("5Min"))
    with pytest.raises(UnsupportedCapabilityError, match="no historical data"):
        ALPACA_CAPABILITIES.require_historical_bars("delayed_sip", Timeframe.parse("5Min"))
    with pytest.raises(UnsupportedCapabilityError, match="2Day"):
        ALPACA_CAPABILITIES.require_historical_bars("iex", Timeframe.parse("2Day"))


def test_require_streaming_checks_data_type():
    ALPACA_CAPABILITIES.require_streaming("iex", DataType.QUOTE)
    bars_only = ALPACA_CAPABILITIES.model_copy(
        update={"streaming_data_types": frozenset({DataType.BAR})}
    )
    with pytest.raises(UnsupportedCapabilityError, match="does not stream quotes"):
        bars_only.require_streaming("iex", DataType.QUOTE)


def test_historical_scope_is_bars_only_in_this_adapter():
    assert ALPACA_CAPABILITIES.historical_data_types == {DataType.BAR}
    assert ALPACA_CAPABILITIES.streaming_data_types == {
        DataType.BAR,
        DataType.QUOTE,
        DataType.TRADE,
    }


def test_basic_plan_iex_is_streamable_with_symbol_limit():
    access = feed_access(AlpacaFeed.IEX, BASIC)
    assert access.streaming
    assert access.historical
    assert access.stream_symbol_limit == 30
    assert access.requests_per_minute == 200
    assert any("single exchange" in note for note in access.notes)


def test_basic_plan_cannot_stream_sip_and_history_excludes_latest_15_minutes():
    access = feed_access(AlpacaFeed.SIP, BASIC)
    assert access.streaming is False
    assert access.historical is True
    assert access.historical_recency_limit == timedelta(minutes=15)
    with pytest.raises(UnsupportedCapabilityError, match="cannot be streamed"):
        access.require_streaming()


def test_paid_plan_streams_sip_without_limits():
    access = feed_access(AlpacaFeed.SIP, PLUS)
    assert access.streaming
    assert access.historical
    assert access.historical_recency_limit is None
    assert access.stream_symbol_limit is None
    assert access.requests_per_minute == 10_000


@pytest.mark.parametrize("plan", [BASIC, PLUS])
def test_unverified_delayed_sip_access_is_denied_not_assumed(plan):
    access = feed_access(AlpacaFeed.DELAYED_SIP, plan)
    assert access.streaming is False
    assert access.historical is False
    assert any("unverified" in note for note in access.notes)


def test_capability_facts_carry_provenance():
    assert ALPACA_CAPABILITIES.verified_on.isoformat() == "2026-10-29"
    assert all(
        url.startswith("https://docs.alpaca.markets/") for url in ALPACA_CAPABILITIES.documentation
    )
    assert ALPACA_CAPABILITIES.max_page_size == 10_000

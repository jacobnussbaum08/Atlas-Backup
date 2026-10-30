"""Standardized schemas. All records below are hand-built test fixtures, not market data."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.data_engine.schemas import (
    DataIngestionJob,
    DataQualityStatus,
    DataSource,
    IngestionJobStatus,
    MarketBar,
    MarketQuote,
    MarketTrade,
    ProviderBar,
    ProviderQuote,
    ProviderTrade,
)
from app.data_engine.timeframes import MINUTE_1

T0 = datetime(2026, 3, 2, 14, 31, tzinfo=UTC)
RECEIVED = T0 + timedelta(seconds=61)


def _bar(**overrides) -> ProviderBar:
    values = {
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": "TEST",
        "source": DataSource.HISTORICAL_REST,
        "timeframe": "1Min",
        "event_time": T0,
        "received_at": RECEIVED,
        "available_at": RECEIVED,
        "open": "100.10",
        "high": "100.50",
        "low": "99.90",
        "close": "100.20",
        "volume": "1200",
        "is_complete": True,
    } | overrides
    return ProviderBar(**values)


def test_bar_keeps_exact_decimals_and_canonical_timeframe():
    bar = _bar()
    assert bar.open == Decimal("100.10")
    assert isinstance(bar.close, Decimal)
    assert bar.timeframe == MINUTE_1
    assert bar.revision == 0


def test_missing_volume_stays_none_and_zero_stays_zero():
    assert _bar(volume=None).volume is None
    assert _bar(volume="0").volume == Decimal(0)
    assert _bar(volume=None).volume != _bar(volume="0").volume


def test_optional_fields_default_to_missing_not_zero():
    bar = _bar()
    assert bar.vwap is None
    assert bar.trade_count is None
    assert bar.session is None


def test_timestamps_are_converted_to_utc():
    eastern = timezone(timedelta(hours=-5))
    bar = _bar(event_time=datetime(2026, 3, 2, 9, 31, tzinfo=eastern))
    assert bar.event_time == T0
    assert bar.event_time.tzinfo is UTC


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError, match="naive"):
        _bar(event_time=datetime(2026, 3, 2, 14, 31))  # noqa: DTZ001


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_prices_are_rejected(value):
    with pytest.raises(ValidationError):
        _bar(close=value)


def test_available_cannot_precede_receipt():
    with pytest.raises(ValidationError, match="available_at"):
        _bar(available_at=RECEIVED - timedelta(seconds=1))


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        _bar(dividend="1")


def test_semantic_checks_are_left_to_quality_engine():
    """An impossible OHLC still parses so the quality engine can quarantine it with a reason."""
    bar = _bar(high="99.00", low="101.00")
    assert bar.high < bar.low


def test_records_are_immutable():
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = Decimal("1")  # type: ignore[misc]  # mutation is the point of the test


def test_market_bar_adds_asset_identity_and_quality():
    asset_id = uuid.uuid4()
    market_bar = MarketBar.from_provider(_bar(), asset_id, DataQualityStatus.VALID)

    assert market_bar.asset_id == asset_id
    assert market_bar.quality_status is DataQualityStatus.VALID
    assert market_bar.provider_symbol == "TEST"
    assert MarketBar.from_provider(_bar(), asset_id).quality_status is DataQualityStatus.UNCHECKED


def test_quote_and_trade_optional_fields():
    common = {
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": "TEST",
        "source": DataSource.LIVE_STREAM,
        "event_time": T0,
        "received_at": RECEIVED,
        "available_at": RECEIVED,
    }
    quote = ProviderQuote(**common, bid_price="10.00", ask_price="10.02", conditions=["R"])
    trade = ProviderTrade(**common, price="10.01")

    assert quote.bid_size is None
    assert quote.conditions == ("R",)
    assert trade.size is None
    assert trade.exchange is None

    asset_id = uuid.uuid4()
    assert MarketQuote.from_provider(quote, asset_id).asset_id == asset_id
    assert MarketTrade.from_provider(trade, asset_id).price == Decimal("10.01")


def test_ingestion_job_requires_ordered_range_and_symbols():
    base = {
        "job_id": uuid.uuid4(),
        "provider": "alpaca",
        "feed": "iex",
        "data_type": "BAR",
        "provider_symbols": ("TEST",),
        "timeframe": "1Day",
        "range_start": T0,
        "range_end": T0 + timedelta(days=1),
        "status": IngestionJobStatus.PENDING,
        "created_at": T0,
    }
    assert DataIngestionJob(**base).records_stored == 0
    with pytest.raises(ValidationError, match="range_end"):
        DataIngestionJob(**base | {"range_end": T0})
    with pytest.raises(ValidationError):
        DataIngestionJob(**base | {"provider_symbols": ()})

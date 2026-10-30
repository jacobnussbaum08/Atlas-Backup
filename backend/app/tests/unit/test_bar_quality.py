"""Data-quality rules, availability policy and request validation (constructed fixtures)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data_engine.availability import conservative_available_at
from app.data_engine.enums import (
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.data_engine.historical import HistoricalFetchRequest
from app.data_engine.reference_assets import (
    DEFAULT_REFERENCE_FILE,
    ReferenceDataError,
    load_reference_assets,
)
from app.data_engine.schemas import ProviderBar
from app.data_engine.timeframes import Timeframe, bar_interval_end, is_aligned
from app.data_engine.validation import assess_bar, check_request_consistency, same_observation
from app.database.models import AssetClass

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
RECEIVED = datetime(2026, 3, 3, tzinfo=UTC)


def _bar(**overrides) -> ProviderBar:
    values = {
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": "AAPL",
        "source": DataSource.HISTORICAL_REST,
        "timeframe": "1Min",
        "adjustment": PriceAdjustment.RAW,
        "event_time": T0,
        "received_at": RECEIVED,
        "available_at": RECEIVED,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "10",
        "vwap": "100.2",
        "trade_count": 3,
        "is_complete": True,
    } | overrides
    return ProviderBar(**values)


def _types(assessment) -> set[DataQualityEventType]:
    return {issue.event_type for issue in assessment.issues}


# ------------------------------------------------------------------ OHLC and prices
def test_valid_bar():
    assessment = assess_bar(_bar())
    assert assessment.status is DataQualityStatus.VALID
    assert assessment.issues == ()


@pytest.mark.parametrize(
    ("overrides", "violation"),
    [
        ({"high": "99.5"}, "high < open"),
        ({"high": "100.4"}, "high < close"),
        ({"low": "100.1"}, "low > open"),
        ({"open": "99", "low": "100.6", "high": "101"}, "low > close"),
        ({"high": "98", "low": "99", "open": "98", "close": "98"}, "high < low"),
    ],
)
def test_impossible_ohlc_is_quarantined_not_corrected(overrides, violation):
    bar = _bar(**overrides)
    assessment = assess_bar(bar)
    assert assessment.status is DataQualityStatus.QUARANTINED
    issue = next(i for i in assessment.issues if i.event_type is DataQualityEventType.INVALID_OHLC)
    assert violation in issue.message
    assert issue.details["high"] == str(bar.high)  # original values preserved


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_non_positive_prices_are_quarantined(field):
    assessment = assess_bar(_bar(**{field: "0"}))
    assert assessment.status is DataQualityStatus.QUARANTINED
    assert DataQualityEventType.INVALID_PRICE in _types(assessment)


def test_negative_volume_and_trade_count_are_quarantined():
    assert assess_bar(_bar(volume="-1")).status is DataQualityStatus.QUARANTINED
    assert assess_bar(_bar(trade_count=-1)).status is DataQualityStatus.QUARANTINED


def test_missing_volume_warns_but_is_not_zero_filled():
    assessment = assess_bar(_bar(volume=None))
    assert assessment.status is DataQualityStatus.VALID
    assert DataQualityEventType.MISSING_FIELD in _types(assessment)


def test_zero_volume_is_valid():
    assert assess_bar(_bar(volume="0")).status is DataQualityStatus.VALID


def test_vwap_outside_range_warns_and_non_positive_vwap_quarantines():
    outside = assess_bar(_bar(vwap="105"))
    assert outside.status is DataQualityStatus.VALID
    assert DataQualityEventType.INVALID_PRICE in _types(outside)
    assert assess_bar(_bar(vwap="0")).status is DataQualityStatus.QUARANTINED


def test_incomplete_bar_is_labelled():
    assert assess_bar(_bar(is_complete=False)).status is DataQualityStatus.INCOMPLETE


def test_quarantine_takes_precedence_over_incomplete():
    assert assess_bar(_bar(is_complete=False, high="1")).status is DataQualityStatus.QUARANTINED


# ------------------------------------------------------------------ timeframe consistency
@pytest.mark.parametrize(
    ("timeframe", "at", "aligned"),
    [
        ("1Min", T0, True),
        ("1Min", T0 + timedelta(seconds=30), False),
        ("5Min", T0 + timedelta(minutes=5), True),
        ("5Min", T0 + timedelta(minutes=2), False),
        ("15Min", T0 + timedelta(minutes=15), True),
        ("1Hour", datetime(2026, 3, 2, 14, 0, tzinfo=UTC), True),
        ("1Hour", T0, False),
        ("1Day", datetime(2026, 3, 2, 5, 0, tzinfo=UTC), True),  # midnight EST
        ("1Day", datetime(2026, 7, 1, 4, 0, tzinfo=UTC), True),  # midnight EDT
        ("1Day", datetime(2026, 3, 2, 0, 0, tzinfo=UTC), False),  # UTC midnight
    ],
)
def test_alignment(timeframe, at, aligned):
    assert is_aligned(at, Timeframe.parse(timeframe)) is aligned


def test_misaligned_bars_are_quarantined():
    assessment = assess_bar(_bar(timeframe="5Min", event_time=T0 + timedelta(minutes=2)))
    assert assessment.status is DataQualityStatus.QUARANTINED
    assert DataQualityEventType.TIMEFRAME_MISALIGNED in _types(assessment)


def test_daily_interval_end_handles_dst():
    # 2026-03-08 is the US spring-forward day: that NY "day" lasts 23 hours.
    start = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert bar_interval_end(start, Timeframe.parse("1Day")) == datetime(
        2026, 3, 9, 4, 0, tzinfo=UTC
    )
    assert bar_interval_end(T0, Timeframe.parse("1Month")) is None


# ------------------------------------------------------------------ request consistency
def _consistency(bar: ProviderBar, symbol: str = "AAPL") -> DataQualityEventType | None:
    issue = check_request_consistency(
        bar,
        expected_symbol=symbol,
        expected_feed="iex",
        range_start=T0,
        range_end=T0 + timedelta(hours=1),
    )
    return None if issue is None else issue.event_type


def test_request_consistency_rejections():
    assert _consistency(_bar()) is None
    assert _consistency(_bar(), symbol="SPY") is DataQualityEventType.UNEXPECTED_SYMBOL
    assert (
        _consistency(_bar(event_time=T0 + timedelta(hours=2)))
        is DataQualityEventType.OUT_OF_REQUESTED_RANGE
    )
    assert _consistency(_bar(feed="sip")) is DataQualityEventType.REJECTED_MESSAGE


def test_same_observation_compares_every_value():
    bar = _bar()
    assert same_observation(bar, _bar())
    assert not same_observation(bar, _bar(close="100.6"))
    assert not same_observation(bar, _bar(volume=None))
    assert not same_observation(bar, _bar(is_complete=False))


# ------------------------------------------------------------------ availability policy
def test_backfilled_bars_become_usable_only_after_interval_plus_settlement():
    assert conservative_available_at(_bar()) == T0 + timedelta(minutes=1, seconds=60)
    hourly = _bar(timeframe="1Hour", event_time=datetime(2026, 3, 2, 14, tzinfo=UTC))
    assert conservative_available_at(hourly) == datetime(2026, 3, 2, 15, 1, tzinfo=UTC)


def test_daily_bars_usable_after_new_york_midnight_and_feed_delay_applies():
    daily = _bar(timeframe="1Day", event_time=datetime(2026, 3, 2, 5, tzinfo=UTC))
    assert conservative_available_at(daily) == datetime(2026, 3, 3, 5, tzinfo=UTC)
    assert conservative_available_at(_bar(), timedelta(minutes=15)) == T0 + timedelta(minutes=17)


def test_retrieval_time_is_never_used_as_availability():
    bar = _bar()
    assert conservative_available_at(bar) != bar.received_at
    assert bar.original_available_at is None


def test_backfill_cannot_claim_original_availability():
    with pytest.raises(ValidationError, match="original_available_at"):
        _bar(original_available_at=T0 + timedelta(minutes=1))


# ------------------------------------------------------------------ fetch request
def _fetch(**overrides) -> HistoricalFetchRequest:
    values = {
        "symbols": ("AAPL", "SPY"),
        "timeframe": "1Day",
        "start": T0,
        "end": T0 + timedelta(days=5),
    } | overrides
    return HistoricalFetchRequest(**values)


def test_fetch_request_validation():
    assert _fetch().timeframe == Timeframe.parse("1Day")
    with pytest.raises(ValidationError, match="either symbols or asset_ids"):
        _fetch(symbols=())
    with pytest.raises(ValidationError, match="end must be after start"):
        _fetch(end=T0)
    with pytest.raises(ValidationError, match="duplicate"):
        _fetch(symbols=("AAPL", "AAPL"))
    with pytest.raises(ValidationError, match="upper-case"):
        _fetch(symbols=("aapl",))
    with pytest.raises(ValidationError, match="at most 20"):
        _fetch(symbols=tuple(f"S{i}" for i in range(21)))
    with pytest.raises(ValidationError, match="invalid timeframe"):
        _fetch(timeframe="2 days")


# ------------------------------------------------------------------ reference data file
def test_shipped_reference_file_has_the_initial_universe():
    reference = load_reference_assets()
    assert set(reference) == {"AAPL", "SPY"}
    assert reference["AAPL"].asset_class is AssetClass.EQUITY
    assert reference["SPY"].asset_class is AssetClass.ETF
    spy, aapl = reference["SPY"].listing("alpaca"), reference["AAPL"].listing("alpaca")
    assert spy is not None
    assert aapl is not None
    assert spy.symbol == "SPY"
    assert aapl.valid_from.tzinfo is not None
    assert DEFAULT_REFERENCE_FILE.name == "reference_assets.toml"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[[asset]]\nsymbol = 'aapl'\nasset_class = 'EQUITY'\nquote_currency = 'USD'\n", "symbol"),
        ("[[asset]]\nsymbol = 'X'\nasset_class = 'BOND'\nquote_currency = 'USD'\n", "asset_class"),
        ("not = [valid", "not valid TOML"),
        (
            "[[asset]]\nsymbol = 'X'\nasset_class = 'EQUITY'\nquote_currency = 'USD'\n"
            "[[asset]]\nsymbol = 'X'\nasset_class = 'EQUITY'\nquote_currency = 'USD'\n",
            "duplicate",
        ),
    ],
)
def test_invalid_reference_files_are_rejected(tmp_path: Path, content, message):
    path = tmp_path / "ref.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ReferenceDataError, match=message):
        load_reference_assets(path)

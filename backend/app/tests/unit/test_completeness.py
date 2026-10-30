"""Completeness arithmetic and expectations (2D). Pure functions, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import MarketDataSettings
from app.data_engine.calendar import FeedHours
from app.data_engine.completeness import (
    UnsupportedTimeframeError,
    ceil_bar,
    check_trackable,
    expected,
    floor_bar,
    intersect,
    live_bar_coverage,
    merge,
    rest_coverage,
    settled_before,
    subtract,
    total,
)
from app.data_engine.enums import DataType
from app.data_engine.live import PRICE_CHANGE_WARNING, LiveIngestionService, price_change
from app.data_engine.timeframes import Timeframe

IEX = FeedHours(time(8), time(17), time(17))
M1, M5, D1 = Timeframe.parse("1Min"), Timeframe.parse("5Min"), Timeframe.parse("1Day")
SETTLE = timedelta(seconds=120)


def t(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


# ------------------------------------------------------------------ intervals
def test_merge_joins_touching_and_overlapping():
    assert merge([(t(1, 2), t(1, 3)), (t(1, 1), t(1, 2)), (t(1, 5), t(1, 6))]) == [
        (t(1, 1), t(1, 3)),
        (t(1, 5), t(1, 6)),
    ]


def test_subtract_and_intersect():
    whole = [(t(1, 0), t(1, 10))]
    assert subtract(whole, [(t(1, 2), t(1, 3)), (t(1, 9), t(1, 12))]) == [
        (t(1, 0), t(1, 2)),
        (t(1, 3), t(1, 9)),
    ]
    assert subtract(whole, whole) == []
    assert intersect(whole, (t(1, 5), t(1, 20))) == [(t(1, 5), t(1, 10))]
    assert total([(t(1, 0), t(1, 1)), (t(1, 0), t(1, 2))]) == timedelta(hours=2)


# ------------------------------------------------------------------ expectations
def test_minute_bars_expected_only_in_feed_hours_on_trading_days():
    # Fri 23 Oct .. Mon 26 Oct 2026 (EDT): IEX 08:00-17:00 = 12:00-21:00 UTC.
    got = expected(DataType.BAR, M1, t(23, 0), t(27, 0), IEX)
    assert got == [(t(23, 12), t(23, 21)), (t(26, 12), t(26, 21))]  # no weekend


def test_holiday_and_closure_produce_no_expectation():
    thanksgiving = datetime(2026, 11, 26, tzinfo=UTC)
    assert expected(DataType.BAR, M1, thanksgiving, thanksgiving + timedelta(days=1), IEX) == []
    assert expected(DataType.TRADE, None, t(31, 0), t(31, 23), IEX) == []  # Saturday


def test_daily_bars_expected_per_trading_day_at_new_york_midnight():
    got = expected(DataType.BAR, D1, t(23, 0), t(28, 0), IEX)
    assert got == [(t(23, 4), t(24, 4)), (t(26, 4), t(28, 4))]


def test_bar_expectations_are_aligned_to_the_bar_grid():
    got = expected(DataType.BAR, M5, t(23, 12, 3), t(23, 12, 21), IEX)
    assert got == [(t(23, 12, 5), t(23, 12, 25))]
    assert floor_bar(t(23, 12, 7, 30), M5) == t(23, 12, 5)
    assert ceil_bar(t(23, 12, 5), M5) == t(23, 12, 5)


def test_unsupported_timeframes_are_refused():
    check_trackable(M5)
    check_trackable(D1)
    with pytest.raises(UnsupportedTimeframeError):
        check_trackable(Timeframe.parse("1Week"))


def test_settled_bounds():
    now = t(30, 15, 30, 30)
    assert settled_before(DataType.BAR, M1, now, SETTLE, IEX) == t(30, 15, 28)
    assert settled_before(DataType.TRADE, None, now, SETTLE, IEX) == now - SETTLE
    # Today's daily bar is still forming during the session...
    assert settled_before(DataType.BAR, D1, now, SETTLE, IEX) == t(30, 4)
    # ...and settled once the feed's day closed (17:00 EDT = 21:00 UTC) plus the delay.
    assert settled_before(DataType.BAR, D1, t(30, 21, 3), SETTLE, IEX) == t(31, 4)


# ------------------------------------------------------------------ evidence
def test_rest_coverage_only_proves_settled_data():
    retrieved = t(30, 15, 30)
    got = rest_coverage(DataType.BAR, M1, t(30, 12), t(30, 21), retrieved, SETTLE, IEX)
    assert got == (t(30, 12), t(30, 15, 28))  # bars after 15:28 were not final yet
    full = rest_coverage(DataType.BAR, M1, t(29, 12), t(29, 20, 59), retrieved, SETTLE, IEX)
    assert full is not None
    assert full[1] == t(29, 20, 59) + timedelta(microseconds=1)
    assert rest_coverage(DataType.BAR, M1, t(30, 16), t(30, 17), retrieved, SETTLE, IEX) is None


def test_live_bar_coverage_needs_the_publication_after_subscription():
    # Subscribed 12:05:30: the 12:04 bar was published at 12:05:00, before it.
    # Persisted through 12:10:10: the 12:09 bar (published ~12:10:00) may still be in flight.
    got = live_bar_coverage(t(30, 12, 5, 30), t(30, 12, 10, 10), M1)
    assert got == (t(30, 12, 5), t(30, 12, 9))
    assert live_bar_coverage(t(30, 12, 5, 30), t(30, 12, 6, 0), M1) is None


# ------------------------------------------------------------------ live review flags
def test_price_change_flags_large_moves_only():
    assert price_change(None, Decimal("100")) is None
    assert price_change(Decimal("100"), Decimal("105")) is None
    assert price_change(Decimal("100"), Decimal("111")) == Decimal("0.11")
    assert price_change(Decimal("100"), Decimal("89")) == Decimal("0.11")
    assert Decimal("0.10") == PRICE_CHANGE_WARNING


def _service() -> LiveIngestionService:
    return LiveIngestionService(None, provider="alpaca", feed="iex", clock=None)  # type: ignore[arg-type]


def test_trade_sequence_gaps_are_reported_only_for_dense_sequences():
    service = _service()
    for trade_id in range(1, 30):
        assert service.check_trade_sequence("AAPL", str(trade_id)) is None
    assert service.check_trade_sequence("AAPL", "33") == (30, 32)
    assert service.check_trade_sequence("AAPL", "34") is None
    # Sparse provider ids (as seen for SPY on IEX) never count as a sequence.
    sparse = _service()
    for trade_id in (52983525031243, 52983525495139, 52983525563696):
        assert sparse.check_trade_sequence("SPY", str(trade_id)) is None
    assert sparse.check_trade_sequence("SPY", None) is None


def test_stale_detection_threshold():
    service = LiveIngestionService(
        None,  # type: ignore[arg-type]
        provider="alpaca",
        feed="iex",
        clock=None,  # type: ignore[arg-type]
        stale_after=timedelta(seconds=60),
    )
    assert service.is_stale(t(30, 12, 1, 1), t(30, 12, 0))
    assert not service.is_stale(t(30, 12, 0, 59), t(30, 12, 0))
    assert not _service().is_stale(t(30, 13), t(30, 12))  # disabled


# ------------------------------------------------------------------ settings
def test_recovery_settings_defaults_and_validation():
    settings = MarketDataSettings(_env_file=None)
    assert settings.market_data_recovery_enabled is False
    assert settings.market_data_recovery_timeframes == ("1Min", "1Day")
    assert settings.market_data_recovery_data_types == (DataType.BAR, DataType.TRADE)
    parsed = MarketDataSettings(
        _env_file=None,
        market_data_recovery_data_types="bars, quotes",
        market_data_recovery_timeframes="5Min",
    )
    assert parsed.market_data_recovery_data_types == (DataType.BAR, DataType.QUOTE)
    for bad in (
        {"market_data_recovery_data_types": "bars,ticks"},
        {"market_data_recovery_data_types": "bars,bars"},
        {"market_data_recovery_timeframes": "1Minute"},
        {"market_data_recovery_settle_seconds": 5},
    ):
        with pytest.raises(ValidationError):
            MarketDataSettings(_env_file=None, **bad)

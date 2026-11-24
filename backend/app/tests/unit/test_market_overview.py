"""Dashboard read-model logic (2E): calendar navigation, market status, freshness, change
arithmetic, live-update queue overflow and the database-unavailable error mapping.
Pure functions; no services."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.middleware import request_context_middleware
from app.core.exceptions import register_exception_handlers
from app.data_engine.calendar import (
    FeedHours,
    MarketSession,
    next_regular_open,
    next_session,
    next_trading_day,
    previous_trading_day,
)
from app.data_engine.enums import DataSource
from app.data_engine.live_updates import LiveEvent, Subscription
from app.data_engine.market_overview import (
    Freshness,
    classify_freshness,
    market_status,
    percent_change,
    session_midnight,
)

IEX = FeedHours(time(8), time(17), time(17))
STALE = timedelta(seconds=180)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------------------------ calendar navigation
def test_trading_day_navigation_skips_weekends_and_holidays():
    # Thanksgiving 2026 is Thu 26 Nov; Fri 27 Nov is an early close (a trading day).
    assert next_trading_day(date(2026, 11, 25)).day == date(2026, 11, 27)
    assert previous_trading_day(date(2026, 11, 27)).day == date(2026, 11, 25)
    # Monday's previous trading day is Friday.
    assert previous_trading_day(date(2026, 11, 2)).day == date(2026, 10, 30)


def test_next_regular_open_crosses_the_dst_change():
    # Fri 30 Oct 2026 after the close (EDT); DST ends Sun 1 Nov: Monday opens 14:30 UTC.
    assert next_regular_open(utc(2026, 10, 30, 21)) == utc(2026, 11, 2, 14, 30)
    # Before today's open, the next open is today's.
    assert next_regular_open(utc(2026, 10, 30, 12)) == utc(2026, 10, 30, 13, 30)
    assert next_session(utc(2026, 10, 31, 12)).day == date(2026, 11, 2)


# ------------------------------------------------------------------ market status
def test_market_status_during_the_regular_session():
    status = market_status(utc(2026, 3, 10, 15), "iex", IEX)  # 11:00 EDT, Tuesday
    assert status.session is MarketSession.REGULAR
    assert status.is_regular_session
    assert status.trading_day == date(2026, 3, 10)
    assert status.next_regular_close == utc(2026, 3, 10, 20)
    assert status.next_regular_open == utc(2026, 3, 11, 13, 30)
    assert status.feed_open
    assert status.feed_window_start == utc(2026, 3, 10, 12)
    assert status.feed_window_end == utc(2026, 3, 10, 21)
    assert status.closure is None


def test_market_status_on_a_holiday_and_a_weekend():
    holiday = market_status(utc(2026, 11, 26, 16), "iex", IEX)
    assert holiday.session is MarketSession.CLOSED
    assert holiday.closure == "Thanksgiving Day"
    assert holiday.trading_day is None
    assert not holiday.feed_open
    assert holiday.next_regular_open == utc(2026, 11, 27, 14, 30)
    assert holiday.next_regular_close == utc(2026, 11, 27, 18)  # early close 13:00 EST
    weekend = market_status(utc(2026, 10, 31, 16), "iex", IEX)
    assert weekend.closure == "weekend"
    assert weekend.feed_window_start is None


def test_feed_hours_extend_beyond_the_regular_session():
    status = market_status(utc(2026, 3, 10, 12, 30), "iex", IEX)  # 08:30 EDT pre-market
    assert status.session is MarketSession.PRE_MARKET
    assert status.feed_open
    assert not status.is_regular_session


# ------------------------------------------------------------------ freshness
def test_freshness_classification():
    now = utc(2026, 3, 10, 15)
    recent = now - timedelta(seconds=30)
    live = DataSource.LIVE_STREAM
    rest = DataSource.HISTORICAL_REST
    kw = {"stale_after": STALE}
    assert classify_freshness(None, None, now, is_feed_open=True, **kw) is Freshness.NO_DATA
    assert classify_freshness(recent, live, now, is_feed_open=True, **kw) is Freshness.LIVE
    assert (
        classify_freshness(recent, live, now, is_feed_open=False, **kw) is Freshness.MARKET_CLOSED
    )
    old = now - timedelta(minutes=10)
    assert classify_freshness(old, live, now, is_feed_open=True, **kw) is Freshness.STALE
    assert classify_freshness(recent, rest, now, is_feed_open=True, **kw) is Freshness.HISTORICAL
    delayed = now - timedelta(minutes=15, seconds=30)
    assert (
        classify_freshness(delayed, live, now, is_feed_open=True, delay=timedelta(minutes=15), **kw)
        is Freshness.DELAYED
    )


def test_percent_change_and_daily_bar_timestamp():
    assert percent_change(Decimal("101.50"), Decimal("100.00")) == Decimal("1.5000")
    assert percent_change(Decimal("99"), Decimal("0")) is None
    # Daily bars are stamped at New York midnight: 04:00 UTC in summer, 05:00 in winter.
    assert session_midnight(date(2026, 3, 9)) == utc(2026, 3, 9, 4)
    assert session_midnight(date(2026, 1, 5)) == utc(2026, 1, 5, 5)


# ------------------------------------------------------------------ live update queue
def test_a_client_that_cannot_keep_up_is_told_to_reload():
    async def scenario() -> list[str]:
        sub = Subscription(symbols={uuid.uuid4(): "AAPL"}, queue=asyncio.Queue(maxsize=2))
        for i in range(3):
            sub.offer(LiveEvent("trade", {"i": i}))
        return [sub.queue.get_nowait().type for _ in range(sub.queue.qsize())]

    assert asyncio.run(scenario()) == ["resync"]


def test_live_event_wire_format():
    assert LiveEvent("bar", {"a": 1}).encode() == 'event: bar\ndata: {"a":1}\n\n'


# ------------------------------------------------------------------ error mapping
def test_unreachable_database_is_reported_as_503_not_500():
    app = FastAPI()
    app.middleware("http")(request_context_middleware)
    register_exception_handlers(app)

    @app.get("/db")
    def db() -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    response = TestClient(app).get("/db")
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "dependency_unavailable"
    assert "refused" not in body["message"]  # no internal details

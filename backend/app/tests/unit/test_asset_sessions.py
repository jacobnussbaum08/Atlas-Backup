"""Exchange-aware sessions: holidays, early closes, DST, weekends, settled sessions."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest

from app.asset_discovery.sessions import (
    CALENDARS,
    UnsupportedExchangeError,
    calendar_for,
    completed_sessions,
    describe,
    session_at,
    trading_day,
)
from app.data_engine.calendar import MarketSession

US_VENUES = ("XNYS", "XNAS", "ARCX", "XASE", "BATS")


@pytest.mark.parametrize("code", US_VENUES)
def test_every_us_venue_declares_its_own_calendar(code):
    cal = calendar_for(code)
    assert cal.code == code
    assert cal.timezone == "America/New_York"
    assert cal.notes  # each venue says where its schedule comes from


def test_unknown_or_missing_calendar_is_unsupported():
    with pytest.raises(UnsupportedExchangeError):
        calendar_for(None)
    with pytest.raises(UnsupportedExchangeError):
        calendar_for("OTCM")
    assert "OTCM" not in CALENDARS


def test_holiday_weekend_and_early_close():
    cal = calendar_for("XNAS")
    assert trading_day(cal, date(2026, 11, 26)) is None  # Thanksgiving
    assert trading_day(cal, date(2026, 11, 28)) is None  # Saturday
    friday = trading_day(cal, date(2026, 11, 27))
    assert friday is not None
    assert friday.early_close
    assert friday.regular_close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 EST


def test_daylight_saving_changes_move_the_utc_session():
    cal = calendar_for("XNYS")
    before = trading_day(cal, date(2026, 3, 6))  # EST
    after = trading_day(cal, date(2026, 3, 9))  # EDT (DST began 8 March)
    assert before is not None
    assert after is not None
    assert before.regular_open == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert after.regular_open == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    autumn = trading_day(cal, date(2026, 11, 2))  # EST again (DST ended 1 November)
    assert autumn is not None
    assert autumn.regular_open == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_session_labels_use_the_venue_time_zone():
    cal = calendar_for("ARCX")
    assert session_at(cal, datetime(2026, 3, 10, 13, 0, tzinfo=UTC)) is MarketSession.PRE_MARKET
    assert session_at(cal, datetime(2026, 3, 10, 15, 0, tzinfo=UTC)) is MarketSession.REGULAR
    assert session_at(cal, datetime(2026, 3, 10, 21, 0, tzinfo=UTC)) is MarketSession.AFTER_HOURS
    assert session_at(cal, datetime(2026, 3, 14, 15, 0, tzinfo=UTC)) is MarketSession.CLOSED


def test_completed_sessions_exclude_the_running_and_unsettled_session():
    cal = calendar_for("XNAS")
    during = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)  # Tuesday, session running
    days = completed_sessions(cal, during, 3)
    assert [d.day for d in days] == [date(2026, 3, 5), date(2026, 3, 6), date(2026, 3, 9)]
    just_closed = datetime(2026, 3, 10, 20, 30, tzinfo=UTC)  # 30 min after close
    assert completed_sessions(cal, just_closed, 1)[0].day == date(2026, 3, 9)
    settled = datetime(2026, 3, 10, 21, 30, tzinfo=UTC)
    assert completed_sessions(cal, settled, 1)[0].day == date(2026, 3, 10)


def test_completed_sessions_skip_holidays_and_weekends():
    cal = calendar_for("XNYS")
    monday = datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
    days = [d.day for d in completed_sessions(cal, monday, 3)]
    assert days == [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]


def test_describe_explains_closures_and_names_the_next_open():
    cal = calendar_for("XNAS")
    state = describe(cal, datetime(2026, 11, 26, 16, 0, tzinfo=UTC))
    assert state.closure == "Thanksgiving Day"
    assert not state.is_trading_day
    assert state.next_regular_open == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert state.halts_known is False
    assert any("halt" in n.lower() for n in state.notes)
    weekend = describe(cal, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    assert weekend.closure == "weekend"
    assert weekend.previous_trading_day == date(2026, 10, 30)


def test_unverified_years_are_marked():
    cal = calendar_for("XNYS")
    day = trading_day(cal, date(2031, 3, 4))
    assert day is not None
    assert not day.verified
    assert describe(cal, datetime(2026, 3, 4, 15, tzinfo=UTC)).verified


def test_completed_sessions_count_is_exact():
    cal = calendar_for("BATS")
    days = completed_sessions(cal, datetime(2026, 10, 30, 23, tzinfo=UTC), 60)
    assert len(days) == 60
    assert all(b.day > a.day for a, b in itertools.pairwise(days))
    assert days[-1].day == date(2026, 10, 30)
    assert days[0].day > date(2026, 10, 30) - timedelta(days=100)

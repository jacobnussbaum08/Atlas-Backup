"""Completed periods and proposal expiry on the exchange calendar (Phase 4A).

A daily period is a regular session; a weekly period is an ISO week of sessions. A period
is *completed* at a decision time once its last session's regular close plus the settle
delay has passed: bars of periods still in progress are never strategy inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from app.asset_discovery.sessions import (
    ExchangeCalendar,
    SessionDay,
    completed_sessions,
    local_date,
    next_trading_day,
    trading_day,
)
from app.feature_engine.registry import DAILY, WEEKLY
from app.market_intelligence.engine import complete_weeks

WeekKey = tuple[int, int]


def week_key(day: date) -> WeekKey:
    iso = day.isocalendar()
    return (iso.year, iso.week)


def sessions_for(timeframe: str, periods: int) -> int:
    """Daily sessions to load for ``periods`` completed periods (weeks: at most five
    sessions each, plus room for the oldest partial week and holidays)."""
    if timeframe == DAILY:
        return periods
    if timeframe == WEEKLY:
        return (periods + 2) * 5 + 10
    raise ValueError(f"unsupported timeframe {timeframe}")


def completed_daily(
    calendar: ExchangeCalendar, at: datetime, count: int, settle: timedelta
) -> list[SessionDay]:
    return completed_sessions(calendar, at, count, settle)


def completed_weeks(
    calendar: ExchangeCalendar, sessions: Sequence[SessionDay]
) -> dict[WeekKey, list[date]]:
    """Complete ISO weeks among ``sessions`` (oldest first, contiguous) -> their days."""
    days = [s.day for s in sessions]
    complete = complete_weeks(calendar, days)
    weeks: dict[WeekKey, list[date]] = {}
    for day in days:
        key = week_key(day)
        if key in complete:
            weeks.setdefault(key, []).append(day)
    return weeks


def expires_at(
    calendar: ExchangeCalendar, timeframe: str, signal_at: datetime, periods: int
) -> datetime:
    """Regular close of the ``periods``-th period ending after ``signal_at``: a daily
    proposal with ``periods=1`` is valid until the close of the next session that closes
    after the signal; a weekly one until the close of that week's last session."""
    if periods < 1:
        raise ValueError("periods must be at least 1")
    if timeframe not in (DAILY, WEEKLY):
        raise ValueError(f"unsupported timeframe {timeframe}")
    session = trading_day(calendar, local_date(calendar, signal_at))
    if session is None:
        session = next_trading_day(calendar, local_date(calendar, signal_at))
    remaining = periods
    while True:
        nxt = next_trading_day(calendar, session.day)
        ends_period = timeframe == DAILY or week_key(nxt.day) != week_key(session.day)
        if ends_period and session.regular_close > signal_at:
            remaining -= 1
            if remaining == 0:
                return session.regular_close
        session = nxt


__all__ = [
    "completed_daily",
    "completed_weeks",
    "expires_at",
    "sessions_for",
    "week_key",
]

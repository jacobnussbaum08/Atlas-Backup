"""Exchange-aware market sessions (Phase 3A), built on the 2D NYSE calendar.

Each listing venue (``exchanges`` row, by MIC) names a ``calendar_code``. A calendar is a
holiday/early-close rule source plus the venue's own session hours, in the venue's time
zone. US equity venues currently share NYSE's published holiday and early-close schedule,
which ``app.data_engine.calendar`` derives from rules (verified 2025-2027); each venue
still declares its own hours here, so a venue that diverges gets its own entry rather
than inheriting NYSE's by assumption.

* Extended-hours windows are the generic US 04:00-20:00 ET bounds (17:00 on early-close
  days) used since 2C; individual venues' early/late sessions are not modelled.
* Trading halts are not known to Atlas: no halt feed is integrated (the stream's
  trading-status channel is not subscribed). Responses say so.
* Overnight (24x5) sessions are not modelled. Alpaca's ``overnight_tradable`` attribute is
  kept as reference data only.
* A venue without a calendar (OTC, unmapped codes) is unsupported: Atlas cannot tell a
  closed market from missing data there.

All inputs and outputs are UTC; local dates are the venue's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.data_engine import calendar as nyse
from app.data_engine.calendar import MarketSession
from app.data_engine.errors import UnsupportedCapabilityError


class UnsupportedExchangeError(UnsupportedCapabilityError):
    code = "unsupported_exchange_calendar"


@dataclass(frozen=True)
class SessionHours:
    regular_open: time
    regular_close: time
    early_close: time
    extended_open: time
    extended_close: time
    early_extended_close: time


@dataclass(frozen=True)
class ExchangeCalendar:
    code: str
    name: str
    timezone: str
    hours: SessionHours
    holiday_rules: str
    verified_years: range
    notes: tuple[str, ...] = field(default=())

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


_US_HOURS = SessionHours(
    regular_open=nyse.REGULAR_OPEN,
    regular_close=nyse.REGULAR_CLOSE,
    early_close=nyse.EARLY_CLOSE,
    extended_open=nyse.PRE_MARKET_OPEN,
    extended_close=nyse.AFTER_HOURS_CLOSE,
    early_extended_close=nyse.EARLY_CLOSE_AFTER_HOURS_CLOSE,
)
_NYSE_RULES = "NYSE holiday and early-close rules (app.data_engine.calendar)"
_SHARED_SCHEDULE = (
    "Uses NYSE's holiday and early-close schedule, which this venue publishes identically "
    "for the verified years; regular session 09:30-16:00 ET."
)


def _us_venue(code: str, name: str, *notes: str) -> ExchangeCalendar:
    return ExchangeCalendar(
        code=code,
        name=name,
        timezone="America/New_York",
        hours=_US_HOURS,
        holiday_rules=_NYSE_RULES,
        verified_years=nyse.VERIFIED_YEARS,
        notes=notes,
    )


CALENDARS: dict[str, ExchangeCalendar] = {
    "XNYS": _us_venue("XNYS", "New York Stock Exchange", "NYSE's own published calendar."),
    "XNAS": _us_venue("XNAS", "Nasdaq Stock Market", _SHARED_SCHEDULE),
    "ARCX": _us_venue("ARCX", "NYSE Arca", _SHARED_SCHEDULE),
    "XASE": _us_venue("XASE", "NYSE American", _SHARED_SCHEDULE),
    "BATS": _us_venue("BATS", "Cboe BZX Exchange", _SHARED_SCHEDULE),
}


def calendar_for(calendar_code: str | None) -> ExchangeCalendar:
    if calendar_code is None or calendar_code not in CALENDARS:
        raise UnsupportedExchangeError(
            f"no verified session calendar for {calendar_code or 'this venue'}"
        )
    return CALENDARS[calendar_code]


@dataclass(frozen=True)
class SessionDay:
    day: date
    regular_open: datetime
    regular_close: datetime
    early_close: bool
    verified: bool


@dataclass(frozen=True)
class SessionState:
    calendar_code: str
    timezone: str
    at: datetime
    local_date: date
    session: MarketSession
    is_trading_day: bool
    closure: str | None
    today: SessionDay | None
    next_regular_open: datetime
    previous_trading_day: date
    verified: bool
    halts_known: bool = False
    notes: tuple[str, ...] = ()


def _utc(cal: ExchangeCalendar, day: date, local: time) -> datetime:
    return datetime.combine(day, local, tzinfo=cal.zone).astimezone(UTC)


def local_date(cal: ExchangeCalendar, at: datetime) -> date:
    return at.astimezone(cal.zone).date()


def trading_day(cal: ExchangeCalendar, day: date) -> SessionDay | None:
    """The venue's trading day on its local date ``day``, or None when closed."""
    # The holiday rules are NYSE's for every calendar defined today (see module docstring).
    if nyse.closure_reason(day) is not None:
        return None
    early = day in nyse.early_closes(day.year)
    close = cal.hours.early_close if early else cal.hours.regular_close
    return SessionDay(
        day=day,
        regular_open=_utc(cal, day, cal.hours.regular_open),
        regular_close=_utc(cal, day, close),
        early_close=early,
        verified=day.year in cal.verified_years,
    )


def session_at(cal: ExchangeCalendar, at: datetime) -> MarketSession:
    day = trading_day(cal, local_date(cal, at))
    if day is None:
        return MarketSession.CLOSED
    if day.regular_open <= at < day.regular_close:
        return MarketSession.REGULAR
    if _utc(cal, day.day, cal.hours.extended_open) <= at < day.regular_open:
        return MarketSession.PRE_MARKET
    late = cal.hours.early_extended_close if day.early_close else cal.hours.extended_close
    if day.regular_close <= at < _utc(cal, day.day, late):
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


_MAX_SEARCH_DAYS = 14


def _step(cal: ExchangeCalendar, start: date, direction: int) -> SessionDay:
    day = start
    for _ in range(_MAX_SEARCH_DAYS):
        day += timedelta(days=direction)
        found = trading_day(cal, day)
        if found is not None:
            return found
    raise RuntimeError(f"no trading day within {_MAX_SEARCH_DAYS} days of {start}")


def next_trading_day(cal: ExchangeCalendar, after: date) -> SessionDay:
    return _step(cal, after, 1)


def previous_trading_day(cal: ExchangeCalendar, before: date) -> SessionDay:
    return _step(cal, before, -1)


def completed_sessions(
    cal: ExchangeCalendar, as_of: datetime, count: int, settle: timedelta = timedelta(hours=1)
) -> list[SessionDay]:
    """The ``count`` most recent sessions whose regular close + ``settle`` is <= ``as_of``,
    oldest first. Sessions still in progress (or just closed) are never included, so a
    qualification never treats an unfinished daily bar as final."""
    sessions: list[SessionDay] = []
    day = local_date(cal, as_of) + timedelta(days=1)
    while len(sessions) < count:
        found = previous_trading_day(cal, day)
        day = found.day
        if found.regular_close + settle <= as_of:
            sessions.append(found)
    return list(reversed(sessions))


def describe(cal: ExchangeCalendar, at: datetime) -> SessionState:
    today_date = local_date(cal, at)
    today = trading_day(cal, today_date)
    closure = None
    reason = nyse.closure_reason(today_date)
    if reason is not None:
        closure = (
            "weekend"
            if reason is nyse.ClosureReason.WEEKEND
            else nyse.holidays(today_date.year).get(today_date, reason.value.lower())
        )
    if today is not None and at < today.regular_open:
        next_open = today.regular_open
    else:
        next_open = next_trading_day(cal, today_date).regular_open
    return SessionState(
        calendar_code=cal.code,
        timezone=cal.timezone,
        at=at,
        local_date=today_date,
        session=session_at(cal, at),
        is_trading_day=today is not None,
        closure=closure,
        today=today,
        next_regular_open=next_open,
        previous_trading_day=previous_trading_day(cal, today_date).day,
        verified=today_date.year in cal.verified_years,
        notes=(
            *cal.notes,
            "Trading halts are not known: no halt feed is integrated.",
        ),
    )


__all__ = [
    "CALENDARS",
    "ExchangeCalendar",
    "SessionDay",
    "SessionState",
    "UnsupportedExchangeError",
    "calendar_for",
    "completed_sessions",
    "describe",
    "local_date",
    "next_trading_day",
    "previous_trading_day",
    "session_at",
    "trading_day",
]

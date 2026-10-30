"""US equity market calendar (Deliverable 2D).

Completeness checks must tell a missing observation from a closed market, so Atlas needs
to know which days the US equity market trades and when.

Holidays and early closes are generated from NYSE's published rules and checked against
NYSE's published calendar for 2025-2027 (unit tests pin every date). Outside
``VERIFIED_YEARS`` the same rules are used, but ``is_verified`` is False so callers can
say so. Unscheduled closures (e.g. a national day of mourning) cannot be derived from
rules; they are listed in ``SPECIAL_CLOSURES`` with their source.

Rules (NYSE):
* New Year's Day: Jan 1; Sunday -> Monday; Saturday -> not observed.
* Martin Luther King Jr. Day / Washington's Birthday: 3rd Monday of Jan / Feb.
* Good Friday: two days before Western Easter.
* Memorial Day: last Monday of May. Labor Day: 1st Monday of Sep.
* Juneteenth (from 2022), Independence Day, Christmas: Saturday -> Friday,
  Sunday -> Monday.
* Thanksgiving: 4th Thursday of Nov.
* Early close (13:00 ET): the day after Thanksgiving; Dec 24 when it is Monday-Thursday;
  Jul 3 when it is Monday-Thursday (i.e. Jul 4 falls Tuesday-Friday).

All arguments and results are timezone-aware; local times are America/New_York.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache

from app.data_engine.timeframes import US_EQUITY_TIMEZONE

VERIFIED_YEARS = range(2025, 2028)  # checked against nyse.com/markets/hours-calendars

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
# Generic US extended hours (used for the session label, not for data expectations).
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)
# NYSE: on early-close days the Arca/American late sessions close at 17:00.
EARLY_CLOSE_AFTER_HOURS_CLOSE = time(17, 0)

# Closures that no rule predicts. Each entry names its source.
SPECIAL_CLOSURES: dict[date, str] = {
    # NYSE closed for the National Day of Mourning for former President Jimmy Carter.
    date(2025, 1, 9): "National Day of Mourning (President Carter)",
}


class MarketSession(StrEnum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"


class ClosureReason(StrEnum):
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    SPECIAL_CLOSURE = "SPECIAL_CLOSURE"


@dataclass(frozen=True)
class TradingDay:
    day: date
    regular_open: datetime  # UTC
    regular_close: datetime  # UTC
    early_close: bool
    verified: bool

    def at(self, local: time) -> datetime:
        return _utc(self.day, local)


@dataclass(frozen=True)
class FeedHours:
    """Local hours in which a feed can publish observations on a trading day.

    ``close_on_early_close`` is the end on early-close days. Atlas only *expects* data in
    these windows; data outside them is still accepted.
    """

    open: time
    close: time
    close_on_early_close: time

    def window(self, day: TradingDay) -> tuple[datetime, datetime]:
        close = self.close_on_early_close if day.early_close else self.close
        return day.at(self.open), day.at(close)


REGULAR_HOURS = FeedHours(REGULAR_OPEN, REGULAR_CLOSE, EARLY_CLOSE)


def _utc(day: date, local: time) -> datetime:
    return datetime.combine(day, local, tzinfo=US_EQUITY_TIMEZONE).astimezone(UTC)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + month // 12, month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    return date(year, month, (h + m - 7 * n + 114) % 31 + 1)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def holidays(year: int) -> dict[date, str]:
    """Full-day NYSE closures of ``year`` (scheduled holidays and special closures)."""
    days: dict[date, str] = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # Saturday: not observed on the previous Friday
        days[_observed(new_year)] = "New Year's Day"
    days[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King, Jr. Day"
    days[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    days[easter(year) - timedelta(days=2)] = "Good Friday"
    days[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        days[_observed(date(year, 6, 19))] = "Juneteenth National Independence Day"
    days[_observed(date(year, 7, 4))] = "Independence Day"
    days[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    days[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    days[_observed(date(year, 12, 25))] = "Christmas Day"
    days.update({d: name for d, name in SPECIAL_CLOSURES.items() if d.year == year})
    return days


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset[date]:
    closes = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    for candidate in (date(year, 12, 24), date(year, 7, 3)):
        if candidate.weekday() <= 3:  # Monday-Thursday
            closes.add(candidate)
    return frozenset(closes - holidays(year).keys())


def closure_reason(day: date) -> ClosureReason | None:
    if day.weekday() >= 5:
        return ClosureReason.WEEKEND
    if day in SPECIAL_CLOSURES:
        return ClosureReason.SPECIAL_CLOSURE
    if day in holidays(day.year):
        return ClosureReason.HOLIDAY
    return None


def is_verified(day: date) -> bool:
    return day.year in VERIFIED_YEARS


def trading_day(day: date) -> TradingDay | None:
    """The trading day ``day`` (a New York calendar date), or None if the market is closed."""
    if closure_reason(day) is not None:
        return None
    early = day in early_closes(day.year)
    return TradingDay(
        day=day,
        regular_open=_utc(day, REGULAR_OPEN),
        regular_close=_utc(day, EARLY_CLOSE if early else REGULAR_CLOSE),
        early_close=early,
        verified=is_verified(day),
    )


def local_date(at: datetime) -> date:
    return at.astimezone(US_EQUITY_TIMEZONE).date()


def trading_days(start: datetime, end: datetime) -> list[TradingDay]:
    """Trading days whose New York date lies in [local_date(start), local_date(end)]."""
    days = []
    current, last = local_date(start), local_date(end)
    while current <= last:
        found = trading_day(current)
        if found is not None:
            days.append(found)
        current += timedelta(days=1)
    return days


def windows(start: datetime, end: datetime, hours: FeedHours) -> list[tuple[datetime, datetime]]:
    """Feed windows on trading days, clipped to [start, end)."""
    result = []
    for day in trading_days(start, end):
        open_, close = hours.window(day)
        lo, hi = max(open_, start), min(close, end)
        if lo < hi:
            result.append((lo, hi))
    return result


def session_at(at: datetime) -> MarketSession:
    """US equity session label at ``at``, holiday- and early-close-aware."""
    day = trading_day(local_date(at))
    if day is None:
        return MarketSession.CLOSED
    if day.regular_open <= at < day.regular_close:
        return MarketSession.REGULAR
    if day.at(PRE_MARKET_OPEN) <= at < day.regular_open:
        return MarketSession.PRE_MARKET
    after_close = EARLY_CLOSE_AFTER_HOURS_CLOSE if day.early_close else AFTER_HOURS_CLOSE
    if day.regular_close <= at < day.at(after_close):
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


def describe_closure(at: datetime) -> str | None:
    """Why the market is closed on ``at``'s New York date, or None if it trades."""
    day = local_date(at)
    reason = closure_reason(day)
    if reason is None:
        return None
    if reason is ClosureReason.WEEKEND:
        return "weekend"
    return holidays(day.year).get(day, reason.value.lower())


__all__ = [
    "REGULAR_HOURS",
    "SPECIAL_CLOSURES",
    "VERIFIED_YEARS",
    "ClosureReason",
    "FeedHours",
    "MarketSession",
    "TradingDay",
    "closure_reason",
    "describe_closure",
    "early_closes",
    "easter",
    "holidays",
    "is_verified",
    "local_date",
    "session_at",
    "trading_day",
    "trading_days",
    "windows",
]

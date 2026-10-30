"""Expected observations and completeness evidence (Deliverable 2D). Pure functions.

Atlas does not assume a bar exists for every minute: providers publish a minute bar only
when the feed had eligible trades, and IEX is sparse. Completeness is therefore judged on
*evidence* that an interval was collected in full, not on the presence of each bar:

    gaps = expected windows - coverage evidence

* **Expected windows** are the feed's publishing hours on trading days (NYSE calendar:
  weekends, holidays and early closes excluded), restricted to intervals that have
  *settled* (ended at least ``settle`` ago, so late trades and bar updates are in).
* **Coverage** is a full REST retrieval of the interval, or (bars only) an uninterrupted,
  fully persisted live stream. Inside covered time, a minute without a bar had no eligible
  activity (thin trading, a halt): it is not missing.

Intervals are half-open ``[start, end)``. For bars they bound bar *start* times; for
trades and quotes, event times.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta

from app.data_engine.calendar import FeedHours, local_date, trading_day, trading_days, windows
from app.data_engine.enums import DataType
from app.data_engine.timeframes import US_EQUITY_TIMEZONE, Timeframe, TimeframeUnit

Interval = tuple[datetime, datetime]

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_MICROSECOND = timedelta(microseconds=1)
# A live minute bar arrives shortly after its minute ends; this much delay is tolerated
# before a minute counts as covered by a live stream.
LIVE_BAR_EMIT_GRACE = timedelta(seconds=15)


class UnsupportedTimeframeError(ValueError):
    pass


# ------------------------------------------------------------------ interval arithmetic
def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of intervals, sorted, with touching intervals joined."""
    result: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def subtract(intervals: Iterable[Interval], removed: Iterable[Interval]) -> list[Interval]:
    """``intervals`` minus ``removed`` (both may be unsorted and overlapping)."""
    cuts = merge(removed)
    result: list[Interval] = []
    for start, end in merge(intervals):
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def intersect(intervals: Iterable[Interval], bounds: Interval) -> list[Interval]:
    lo, hi = bounds
    return [(max(s, lo), min(e, hi)) for s, e in merge(intervals) if min(e, hi) > max(s, lo)]


def total(intervals: Iterable[Interval]) -> timedelta:
    return sum((end - start for start, end in merge(intervals)), timedelta(0))


# ------------------------------------------------------------------ bar grid
def check_trackable(timeframe: Timeframe) -> None:
    """Completeness is tracked for intraday bars and 1Day bars."""
    if timeframe.fixed_duration is None and not (
        timeframe.unit is TimeframeUnit.DAY and timeframe.amount == 1
    ):
        raise UnsupportedTimeframeError(
            f"completeness tracking supports intraday and 1Day bars, not {timeframe}"
        )


def floor_bar(at: datetime, timeframe: Timeframe) -> datetime:
    """Start of the bar containing ``at``."""
    duration = timeframe.fixed_duration
    if duration is not None:
        return at - (at - EPOCH) % duration
    return _local_midnight(local_date(at))


def ceil_bar(at: datetime, timeframe: Timeframe) -> datetime:
    start = floor_bar(at, timeframe)
    return start if start == at else _next_bar(start, timeframe)


def _next_bar(start: datetime, timeframe: Timeframe) -> datetime:
    duration = timeframe.fixed_duration
    if duration is not None:
        return start + duration
    return _local_midnight(local_date(start) + timedelta(days=1))


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=US_EQUITY_TIMEZONE).astimezone(UTC)


# ------------------------------------------------------------------ expectations
def expected(
    data_type: DataType,
    timeframe: Timeframe | None,
    start: datetime,
    end: datetime,
    hours: FeedHours,
) -> list[Interval]:
    """Intervals in [start, end) in which the feed can publish observations.

    Bars: bar-start ranges whose bars overlap the feed's hours (1Day: one bar per trading
    day, starting at New York midnight). Trades/quotes: the feed's hours themselves.
    """
    if data_type is not DataType.BAR:
        return windows(start, end, hours)
    if timeframe is None:
        raise ValueError("bars need a timeframe")
    check_trackable(timeframe)
    # Bar starts in [start, end) are exactly those in [ceil(start), ceil(end)).
    start, end = ceil_bar(start, timeframe), ceil_bar(end, timeframe)
    if timeframe.fixed_duration is None:  # 1Day
        days = []
        for day in trading_days(start, end):
            bar_start = _local_midnight(day.day)
            days.append((bar_start, _local_midnight(day.day + timedelta(days=1))))
        return intersect(days, (start, end))
    bar_ranges = [
        (floor_bar(open_, timeframe), ceil_bar(close, timeframe))
        for open_, close in windows(
            floor_bar(start, timeframe) - timeframe.fixed_duration,  # bars overlapping start
            end,
            hours,
        )
    ]
    return intersect(bar_ranges, (start, end))


def settled_before(
    data_type: DataType,
    timeframe: Timeframe | None,
    now: datetime,
    settle: timedelta,
    hours: FeedHours,
) -> datetime:
    """Observations before the returned bound have settled at ``now``.

    Bars: their interval ended at least ``settle`` ago (1Day: the feed's day closed at
    least ``settle`` ago). Trades/quotes: their event is at least ``settle`` old.
    """
    cutoff = now - settle
    if data_type is not DataType.BAR:
        return cutoff
    if timeframe is None:
        raise ValueError("bars need a timeframe")
    if timeframe.fixed_duration is not None:
        return floor_bar(cutoff, timeframe)
    day = local_date(cutoff)
    found = trading_day(day)
    if found is not None and hours.window(found)[1] > cutoff:
        return _local_midnight(day)  # today's daily bar is still forming
    return _local_midnight(day + timedelta(days=1))


def gaps(expected_intervals: Sequence[Interval], covered: Iterable[Interval]) -> list[Interval]:
    return subtract(expected_intervals, covered)


# ------------------------------------------------------------------ coverage evidence
def rest_coverage(
    data_type: DataType,
    timeframe: Timeframe | None,
    requested_start: datetime,
    requested_end_inclusive: datetime,
    retrieved_at: datetime,
    settle: timedelta,
    hours: FeedHours,
) -> Interval | None:
    """Interval a complete REST retrieval proves, or None.

    Only settled observations count: a bar still forming (or a minute whose late trades
    may not be published yet) at retrieval time is not proven complete.
    """
    end = min(
        requested_end_inclusive + ONE_MICROSECOND,
        settled_before(data_type, timeframe, retrieved_at, settle, hours),
    )
    return (requested_start, end) if end > requested_start else None


def live_bar_coverage(
    subscribed_at: datetime, persisted_through: datetime, timeframe: Timeframe
) -> Interval | None:
    """Bar starts a live stream received in full.

    A minute bar is published just after its minute ends. Bars published after the
    subscription was confirmed and before ``persisted_through`` (minus a grace for
    publication delay) were all received and persisted.
    """
    duration = timeframe.fixed_duration
    if duration is None:
        return None
    start = ceil_bar(subscribed_at - duration, timeframe)
    end = floor_bar(persisted_through - duration - LIVE_BAR_EMIT_GRACE, timeframe) + duration
    return (start, end) if end > start else None


__all__ = [
    "LIVE_BAR_EMIT_GRACE",
    "Interval",
    "UnsupportedTimeframeError",
    "ceil_bar",
    "check_trackable",
    "expected",
    "floor_bar",
    "gaps",
    "intersect",
    "live_bar_coverage",
    "merge",
    "rest_coverage",
    "settled_before",
    "subtract",
    "total",
]

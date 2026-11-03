"""Bars and periods for dataset building (Phase 5A).

:class:`PreloadedBars` serves ``app.strategies.inputs.compute_features`` the same rows the
database query would return (newest revision known at ``data_known_at``, sessions of the
request only) from one read per asset, so a dataset of thousands of decision points does
not issue thousands of identical queries. It only serves the knowledge time it was
loaded for; any other request is refused, so it cannot widen what a feature sees.

Decision periods: a daily period is a session; a weekly period is an ISO week whose last
session closed. The decision time of a period is its last session's regular close plus the
settle delay: the first instant its bar counts as completed and settled.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.asset_discovery.metrics import DailyBar
from app.asset_discovery.sessions import (
    ExchangeCalendar,
    SessionDay,
    local_date,
    next_trading_day,
    previous_trading_day,
    trading_day,
)
from app.database.repositories import universe as reads
from app.feature_engine.registry import DAILY, WEEKLY
from app.strategies.inputs import BarSeries
from app.strategies.periods import week_key


def sessions_between(cal: ExchangeCalendar, first: date, last: date) -> list[SessionDay]:
    """Every session from ``first`` to ``last`` inclusive, oldest first."""
    found: list[SessionDay] = []
    day = first
    while day <= last:
        session = trading_day(cal, day)
        if session is not None:
            found.append(session)
        day += timedelta(days=1)
    return found


def sessions_before(cal: ExchangeCalendar, day: date, count: int) -> date:
    """The day ``count`` sessions before ``day``."""
    for _ in range(count):
        day = previous_trading_day(cal, day).day
    return day


def sessions_after(cal: ExchangeCalendar, day: date, count: int) -> list[SessionDay]:
    found: list[SessionDay] = []
    for _ in range(count):
        session = next_trading_day(cal, day)
        found.append(session)
        day = session.day
    return found


@dataclass(frozen=True)
class DecisionPeriod:
    timeframe: str
    # The period's sessions (one for daily; the week's sessions for weekly).
    sessions: tuple[SessionDay, ...]
    decision_at: datetime

    @property
    def last(self) -> SessionDay:
        return self.sessions[-1]


def _ends_week(cal: ExchangeCalendar, session: SessionDay) -> bool:
    return week_key(next_trading_day(cal, session.day).day) != week_key(session.day)


def decision_periods(
    cal: ExchangeCalendar,
    timeframe: str,
    start: datetime,
    end: datetime,
    settle: timedelta,
) -> list[DecisionPeriod]:
    """Periods whose decision time falls in [start, end], oldest first."""
    first = local_date(cal, start) - timedelta(days=7)
    last = local_date(cal, end)
    days = sessions_between(cal, first, last)
    periods: list[DecisionPeriod] = []
    if timeframe == DAILY:
        for s in days:
            periods.append(DecisionPeriod(DAILY, (s,), s.regular_close + settle))
    elif timeframe == WEEKLY:
        week: list[SessionDay] = []
        for s in days:
            if week and week_key(week[-1].day) != week_key(s.day):
                week = []
            week.append(s)
            if _ends_week(cal, s):
                periods.append(DecisionPeriod(WEEKLY, tuple(week), s.regular_close + settle))
                week = []
    else:
        raise ValueError(f"unsupported timeframe {timeframe}")
    return [p for p in periods if start <= p.decision_at <= end]


def forward_sessions(
    cal: ExchangeCalendar, period: DecisionPeriod, horizon: int
) -> list[SessionDay]:
    """The sessions after ``period`` up to the end of its ``horizon``-th following period."""
    if period.timeframe == DAILY:
        return sessions_after(cal, period.last.day, horizon)
    found: list[SessionDay] = []
    day = period.last.day
    weeks = 0
    while weeks < horizon:
        session = next_trading_day(cal, day)
        found.append(session)
        day = session.day
        if _ends_week(cal, session):
            weeks += 1
    return found


class PreloadedBars:
    """A ``DailyBarSource`` for one knowledge time and bar series, one read per asset."""

    def __init__(
        self,
        session: Session,
        series: BarSeries,
        known_at: datetime,
        first: date,
        last: date,
    ) -> None:
        self._session = session
        self.series = series
        self.known_at = known_at
        self._sessions = sessions_between(series.calendar, first, last)
        self._days = {s.day for s in self._sessions}
        self._bars: dict[uuid.UUID, dict[date, DailyBar]] = {}

    def _load(self, asset_id: uuid.UUID) -> dict[date, DailyBar]:
        loaded = self._bars.get(asset_id)
        if loaded is None:
            rows = reads.daily_bars(
                self._session,
                [asset_id],
                provider=self.series.provider,
                feed=self.series.feed,
                adjustment=self.series.adjustment,
                sessions=self._sessions,
                calendar=self.series.calendar,
                as_of=self._sessions[-1].regular_close,
                known_at=self.known_at,
            )
            loaded = {row.session: row for row in rows}
            self._bars[asset_id] = loaded
        return loaded

    def bar(self, asset_id: uuid.UUID, day: date) -> DailyBar | None:
        if day not in self._days:
            raise ValueError(f"{day} is outside the preloaded range")
        return self._load(asset_id).get(day)

    def __call__(
        self,
        session: Session,
        asset_id: uuid.UUID,
        sessions: Sequence[SessionDay],
        *,
        data_cutoff: datetime,
        known_at: datetime,
        series: BarSeries,
    ) -> Sequence[DailyBar]:
        if known_at != self.known_at or series != self.series:
            raise ValueError("preloaded bars serve one knowledge time and series only")
        missing = [s.day for s in sessions if s.day not in self._days]
        if missing:
            raise ValueError(f"sessions outside the preloaded range: {missing[0]}..")
        bars = self._load(asset_id)
        return [bars[s.day] for s in sessions if s.day in bars]


__all__ = [
    "DecisionPeriod",
    "PreloadedBars",
    "decision_periods",
    "forward_sessions",
    "sessions_after",
    "sessions_before",
    "sessions_between",
]

"""X3.1 (D-244): the replay refuses data that contradicts the session calendar."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.backtesting.data import HistoricalDataset, sessions_between


def dataset(days_by_asset: dict[uuid.UUID, list[date]], first: date) -> HistoricalDataset:
    raw: dict[uuid.UUID, dict[date, Any]] = {
        a: {d: object() for d in days} for a, days in days_by_asset.items()
    }
    return HistoricalDataset(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        settle=timedelta(hours=1),
        provider="alpaca",
        feed="sip",
        first_session=first,
        raw=raw,
    )


A, B = uuid.uuid4(), uuid.uuid4()
FIRST, LAST = date(2018, 11, 26), date(2018, 12, 14)


def test_2018_12_05_is_not_a_session_any_more():
    days = [s.day for s in sessions_between(FIRST, LAST)]
    assert date(2018, 12, 5) not in days
    assert date(2018, 12, 4) in days
    assert date(2018, 12, 6) in days


def test_consistent_data_has_no_conflicts():
    days = [s.day for s in sessions_between(FIRST, LAST)]
    found = dataset({A: days, B: days}, FIRST).calendar_conflicts(LAST)
    assert found == {"sessions_without_any_bar": [], "bars_on_closed_days": []}


def test_one_assets_gap_is_not_a_calendar_conflict():
    days = [s.day for s in sessions_between(FIRST, LAST)]
    gappy = [d for d in days if d != date(2018, 12, 6)]
    assert not any(dataset({A: days, B: gappy}, FIRST).calendar_conflicts(LAST).values())


def test_a_session_without_any_bar_inside_the_data_is_a_conflict():
    days = [s.day for s in sessions_between(FIRST, LAST) if s.day != date(2018, 12, 6)]
    found = dataset({A: days, B: days}, FIRST).calendar_conflicts(LAST)
    assert found["sessions_without_any_bar"] == ["2018-12-06"]


def test_sessions_outside_the_loaded_data_are_not_conflicts():
    # Data ends 2018-12-07: the later sessions are a shorter history, not a contradiction.
    days = [s.day for s in sessions_between(FIRST, date(2018, 12, 7))]
    assert not any(dataset({A: days}, FIRST).calendar_conflicts(LAST).values())


def test_a_bar_on_a_closed_day_is_a_conflict():
    days = [s.day for s in sessions_between(FIRST, LAST)] + [date(2018, 12, 5)]
    found = dataset({A: days}, FIRST).calendar_conflicts(LAST)
    assert found["bars_on_closed_days"] == ["2018-12-05"]

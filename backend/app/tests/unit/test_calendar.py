"""US equity calendar (2D). Expected dates are NYSE's published calendar for 2025-2027
(nyse.com/markets/hours-calendars, checked 2026-10-30)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.data_engine.calendar import (
    ClosureReason,
    FeedHours,
    MarketSession,
    closure_reason,
    describe_closure,
    early_closes,
    easter,
    holidays,
    is_verified,
    session_at,
    trading_day,
    trading_days,
    windows,
)

NYSE_HOLIDAYS = {
    2025: [
        date(2025, 1, 1),
        date(2025, 1, 9),  # special closure: National Day of Mourning
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
    ],
    2026: [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # Independence Day observed (Jul 4 is a Saturday)
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
    2027: [
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),  # Juneteenth observed (Jun 19 is a Saturday)
        date(2027, 7, 5),  # Independence Day observed (Jul 4 is a Sunday)
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),  # Christmas observed (Dec 25 is a Saturday)
    ],
}
NYSE_EARLY_CLOSES = {
    2025: [date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)],
    2026: [date(2026, 11, 27), date(2026, 12, 24)],
    2027: [date(2027, 11, 26)],
}


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_holidays_match_nyse_published_calendar(year):
    assert sorted(holidays(year)) == NYSE_HOLIDAYS[year]


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_early_closes_match_nyse_published_calendar(year):
    assert sorted(early_closes(year)) == NYSE_EARLY_CLOSES[year]


def test_easter_dates():
    assert easter(2025) == date(2025, 4, 20)
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)


def test_new_year_on_saturday_is_not_observed_on_friday():
    # Jan 1 2022 was a Saturday; NYSE traded on Friday Dec 31 2021.
    assert date(2021, 12, 31) not in holidays(2021)
    assert date(2022, 1, 1) not in holidays(2022) or trading_day(date(2022, 1, 1)) is None
    assert trading_day(date(2021, 12, 31)) is not None


def test_closure_reasons():
    assert closure_reason(date(2026, 10, 31)) is ClosureReason.WEEKEND
    assert closure_reason(date(2026, 11, 26)) is ClosureReason.HOLIDAY
    assert closure_reason(date(2025, 1, 9)) is ClosureReason.SPECIAL_CLOSURE
    assert closure_reason(date(2026, 10, 30)) is None
    assert describe_closure(datetime(2026, 11, 26, 15, tzinfo=UTC)) == "Thanksgiving Day"
    assert describe_closure(datetime(2026, 10, 31, 15, tzinfo=UTC)) == "weekend"


def test_trading_day_times_in_utc_follow_dst():
    summer = trading_day(date(2026, 10, 30))  # EDT
    winter = trading_day(date(2026, 12, 1))  # EST
    assert summer is not None
    assert winter is not None
    assert summer.regular_open == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert winter.regular_open == datetime(2026, 12, 1, 14, 30, tzinfo=UTC)
    early = trading_day(date(2026, 11, 27))
    assert early is not None
    assert early.early_close
    assert early.regular_close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 EST


def test_verification_range_is_reported():
    assert is_verified(date(2026, 5, 1))
    assert not is_verified(date(2031, 5, 1))
    day = trading_day(date(2031, 5, 1))
    assert day is not None
    assert not day.verified


@pytest.mark.parametrize(
    ("at", "session"),
    [
        (datetime(2026, 10, 30, 13, 30, tzinfo=UTC), MarketSession.REGULAR),
        (datetime(2026, 10, 30, 13, 29, tzinfo=UTC), MarketSession.PRE_MARKET),
        (datetime(2026, 10, 30, 20, 0, tzinfo=UTC), MarketSession.AFTER_HOURS),
        (datetime(2026, 10, 31, 15, 0, tzinfo=UTC), MarketSession.CLOSED),  # Saturday
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), MarketSession.CLOSED),  # Thanksgiving
        (datetime(2026, 11, 27, 18, 30, tzinfo=UTC), MarketSession.AFTER_HOURS),  # early close
        (datetime(2026, 11, 27, 22, 30, tzinfo=UTC), MarketSession.CLOSED),  # after 17:00 EST
    ],
)
def test_session_labels_are_holiday_and_early_close_aware(at, session):
    assert session_at(at) is session


def test_feed_windows_skip_closed_days_and_clip():
    iex = FeedHours(time(8), time(17), time(17))
    start = datetime(2026, 11, 25, 0, tzinfo=UTC)
    end = datetime(2026, 11, 30, 0, tzinfo=UTC)
    got = windows(start, end, iex)
    # Wed 25th, (Thanksgiving 26th closed), Fri 27th early close; weekend closed.
    assert got == [
        (datetime(2026, 11, 25, 13, tzinfo=UTC), datetime(2026, 11, 25, 22, tzinfo=UTC)),
        (datetime(2026, 11, 27, 13, tzinfo=UTC), datetime(2026, 11, 27, 22, tzinfo=UTC)),
    ]
    clipped = windows(datetime(2026, 11, 25, 15, tzinfo=UTC), end, iex)
    assert clipped[0][0] == datetime(2026, 11, 25, 15, tzinfo=UTC)
    # trading_days works on New York dates: 2026-11-25 00:00 UTC is still the 24th there.
    assert [d.day for d in trading_days(start, end)] == [
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
    ]


# ---------------------------------------------------------------- X3.1 (D-244) history
HISTORICAL_SPECIAL_CLOSURES = [
    date(2001, 9, 11),
    date(2001, 9, 12),
    date(2001, 9, 13),
    date(2001, 9, 14),
    date(2004, 6, 11),
    date(2007, 1, 2),
    date(2012, 10, 29),
    date(2012, 10, 30),
    date(2018, 12, 5),
    date(2025, 1, 9),
]


@pytest.mark.parametrize("day", HISTORICAL_SPECIAL_CLOSURES)
def test_unscheduled_historical_closures_are_not_sessions(day):
    assert trading_day(day) is None
    assert closure_reason(day) is ClosureReason.SPECIAL_CLOSURE
    assert day in holidays(day.year)


def test_bush_day_of_mourning_2018_12_05_is_skipped_by_neighbours():
    from app.data_engine.calendar import next_trading_day, previous_trading_day

    assert previous_trading_day(date(2018, 12, 6)).day == date(2018, 12, 4)
    assert next_trading_day(date(2018, 12, 4)).day == date(2018, 12, 6)
    assert [
        d.day
        for d in trading_days(
            datetime(2018, 12, 3, 15, tzinfo=UTC), datetime(2018, 12, 7, 15, tzinfo=UTC)
        )
    ] == [date(2018, 12, 3), date(2018, 12, 4), date(2018, 12, 6), date(2018, 12, 7)]
    assert str(describe_closure(datetime(2018, 12, 5, 15, tzinfo=UTC))).startswith(
        "National Day of Mourning"
    )


def test_hurricane_sandy_closed_two_days_and_reopened_wednesday():
    assert trading_day(date(2012, 10, 26)) is not None
    assert trading_day(date(2012, 10, 31)) is not None


def test_historical_rules_regressions():
    # New Year's Day on Saturday (2011-01-01): not observed; Friday 2010-12-31 traded.
    assert trading_day(date(2010, 12, 31)) is not None
    # Christmas on Saturday (2010, 2021): observed Friday Dec 24.
    assert trading_day(date(2010, 12, 24)) is None
    assert trading_day(date(2021, 12, 24)) is None
    # Juneteenth: only from 2022 (2021-06-18 traded, 2022-06-20 closed).
    assert trading_day(date(2021, 6, 18)) is not None
    assert trading_day(date(2022, 6, 20)) is None
    # Early closes of the research years.
    assert trading_day(date(2018, 12, 24)).early_close  # type: ignore[union-attr]
    assert trading_day(date(2019, 7, 3)).early_close  # type: ignore[union-attr]
    assert trading_day(date(2016, 11, 25)).early_close  # type: ignore[union-attr]
    assert not trading_day(date(2016, 12, 23)).early_close  # type: ignore[union-attr]


EVIDENCE = (
    __import__("pathlib").Path(__file__).resolve().parents[4]
    / "config/simulation/runs/x31_calendar_evidence.json"
)


# REDACTED_FOR_GITHUB: test of withheld research data removed

"""D-234: measured historical spreads and the optional live-pipeline configuration.

Quotes here are TEST FIXTURES (hand-written values), not market data."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.asset_discovery.enums import MetricStatus
from app.backtesting.config import MeasuredSpreadSpec, PipelineSpec
from app.backtesting.spreads import PointInTimeSpreads, QuoteObs, SpreadPoint, summarize, window_for

D = Decimal
T0 = datetime(2024, 3, 5, 20, 45, tzinfo=UTC)


def q(seconds: float, bid: str | None, ask: str | None) -> QuoteObs:
    return QuoteObs(
        T0 + timedelta(seconds=seconds),
        None if bid is None else D(bid),
        None if ask is None else D(ask),
    )


def test_the_median_is_time_weighted_and_keeps_the_observed_quote() -> None:
    # 1 bp spread stands for 50 s, a 100 bp spread for 10 s: the time-weighted median is
    # 1 bp although half of the quotes are wide.
    quotes = [q(0, "100.00", "100.01"), q(50, "99.50", "100.50"), q(55, "99.50", "100.50")]
    s = summarize(quotes, T0 + timedelta(seconds=60))
    assert s.status is MetricStatus.OK
    assert s.median_bps == D("1.0000")
    assert (s.bid, s.ask) == (D("100.00"), D("100.01"))
    assert s.quote_at == T0
    assert s.tick_median_bps == D("100.0000")  # the unweighted median differs


def test_one_sided_and_crossed_quotes_are_excluded_and_no_quote_is_no_data() -> None:
    quotes = [q(0, None, "10.00"), q(1, "0", "10.00"), q(2, "10.05", "10.00")]
    s = summarize(quotes, T0 + timedelta(seconds=60))
    assert s.status is MetricStatus.NO_DATA
    assert s.observations == 3
    assert s.median_bps is None
    assert summarize([], T0).status is MetricStatus.NO_DATA


def test_the_window_ends_fifteen_minutes_before_the_close_also_on_early_closes() -> None:
    start, end = window_for(date(2024, 3, 5)) or (None, None)
    assert start == datetime(2024, 3, 5, 20, 45, tzinfo=UTC)  # 15:45 EST
    assert end == start + timedelta(seconds=60)
    early = window_for(date(2024, 11, 29))  # the day after Thanksgiving closes at 13:00
    assert early is not None
    assert early[0] == datetime(2024, 11, 29, 17, 45, tzinfo=UTC)
    assert window_for(date(2024, 3, 9)) is None  # a Saturday


def point(at: datetime, bps: str | None, status: MetricStatus = MetricStatus.OK) -> SpreadPoint:
    return SpreadPoint(at, status, None if bps is None else D(bps), None, None, None, 1)


def test_point_in_time_lookup_never_reads_ahead_and_respects_the_maximum_age() -> None:
    a = uuid.uuid4()
    day1, day2 = T0, T0 + timedelta(days=1)
    spreads = PointInTimeSpreads("sip", {a: [point(day1, "3"), point(day2, "9")]})
    age = timedelta(hours=6)
    assert spreads.at(a, day1 - timedelta(seconds=1), age) is None  # before any sample
    found = spreads.at(a, day1 + timedelta(hours=1), age)
    assert found is not None
    assert found.bps == D(3)
    assert spreads.at(a, day1 + timedelta(hours=7), age) is None  # too old, next not yet
    found = spreads.at(a, day2, age)
    assert found is not None
    assert found.bps == D(9)
    assert spreads.at(uuid.uuid4(), day2, age) is None
    assert spreads.coverage() == {
        "measured": 2,
        "window_without_two_sided_quote": 0,
        "no_measurement_within_max_age": 3,
    }


def test_a_no_data_window_is_returned_and_counted_not_skipped() -> None:
    a = uuid.uuid4()
    spreads = PointInTimeSpreads("iex", {a: [point(T0, None, MetricStatus.NO_DATA)]})
    found = spreads.at(a, T0, timedelta(hours=6))
    assert found is not None
    assert found.status is MetricStatus.NO_DATA
    assert spreads.coverage()["window_without_two_sided_quote"] == 1


def test_pipeline_options_are_validated() -> None:
    with pytest.raises(ValidationError):
        MeasuredSpreadSpec(feed="boats")
    with pytest.raises(ValidationError):
        MeasuredSpreadSpec(feed="sip", multiplier=D(0))
    spec = PipelineSpec.model_validate({"coordinator": True, "measured_spreads": {"feed": "iex"}})
    assert spec.measured_spreads is not None
    assert spec.measured_spreads.max_age_hours == 6

"""Qualification measurements: session classification and liquidity/volatility maths.

Bars here are SYNTHETIC test vectors with hand-computable values, not market data.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, time

import pytest

from app.asset_discovery.enums import MetricStatus, SessionDayStatus
from app.asset_discovery.metrics import (
    DailyBar,
    MetricWindows,
    SpreadSummary,
    classify,
    compute,
    spread_measurements,
)
from app.asset_discovery.sessions import calendar_for, completed_sessions

CAL = calendar_for("XNAS")
AS_OF = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)
SESSIONS = completed_sessions(CAL, AS_OF, 60)
A = uuid.UUID(int=1)
B = uuid.UUID(int=2)
WINDOWS = MetricWindows()


def bar(
    day, close=10.0, volume=100_000.0, vwap=None, quality="VALID", high=None, low=None, asset=A
):
    return DailyBar(
        asset_id=asset,
        session=day,
        event_time=datetime.combine(day, time(4), tzinfo=UTC),
        open=close,
        high=high if high is not None else close + 0.5,
        low=low if low is not None else close - 0.5,
        close=close,
        volume=volume,
        vwap=vwap,
        quality_status=quality,
    )


def full(asset=A, **kwargs):
    return [bar(s.day, asset=asset, **kwargs) for s in SESSIONS]


def test_sessions_are_classified_without_confusing_zero_missing_and_closed():
    days = [s.day for s in SESSIONS]
    bars = [bar(d) for d in days[:-6]]
    bars += [
        bar(days[-6], volume=0.0),  # genuine zero volume
        bar(days[-5], volume=None),  # volume not reported
        bar(days[-4], quality="QUARANTINED"),
        bar(days[-3], quality="INCOMPLETE"),  # not final -> missing
    ]
    covered = {A: {days[-2]}}  # evidence: retrieval found no trades
    frame = classify(bars, [A], SESSIONS, covered, {})
    statuses = dict(zip(frame["session"].to_list(), frame["status"].to_list(), strict=True))
    assert statuses[days[-6]] == SessionDayStatus.ZERO_VOLUME.value
    assert statuses[days[-5]] == SessionDayStatus.VOLUME_MISSING.value
    assert statuses[days[-4]] == SessionDayStatus.QUARANTINED.value
    assert statuses[days[-3]] == SessionDayStatus.MISSING.value
    assert statuses[days[-2]] == SessionDayStatus.NO_REPORTED_ACTIVITY.value
    assert statuses[days[-1]] == SessionDayStatus.MISSING.value
    assert len(statuses) == 60  # weekends/holidays never appear as sessions


def test_provider_gaps_are_distinguished_from_unexplained_missing_days():
    days = [s.day for s in SESSIONS]
    frame = classify([], [A], SESSIONS, {}, {A: {days[0]}})
    statuses = dict(zip(frame["session"].to_list(), frame["status"].to_list(), strict=True))
    assert statuses[days[0]] == SessionDayStatus.PROVIDER_GAP.value
    assert statuses[days[1]] == SessionDayStatus.MISSING.value


def test_volume_and_dollar_volume_use_known_volume_only():
    bars = full(close=20.0, volume=50_000.0, vwap=21.0)
    days = [s.day for s in SESSIONS]
    bars[-1] = bar(days[-1], close=20.0, volume=None, vwap=21.0)  # missing volume: excluded
    result = compute(bars, [A], SESSIONS)[A]
    adv = result.values["avg_daily_volume"]
    assert adv.status is MetricStatus.OK
    assert adv.value == pytest.approx(50_000.0)
    assert adv.observations == 19
    assert result.value("median_dollar_volume") == pytest.approx(50_000 * 21.0)  # VWAP price
    assert result.value("zero_volume_ratio") == 0.0
    assert result.value("history_coverage") == 1.0


def test_dollar_volume_falls_back_to_close_without_vwap():
    result = compute(full(close=8.0, volume=1_000.0), [A], SESSIONS)[A]
    assert result.value("avg_dollar_volume") == pytest.approx(8_000.0)


def test_true_zero_volume_counts_as_zero_and_consistency_reflects_it():
    days = [s.day for s in SESSIONS]
    bars = full(volume=1_000.0)
    for d in days[-4:]:
        bars[[b.session for b in bars].index(d)] = bar(d, volume=0.0)
    result = compute(bars, [A], SESSIONS)[A]
    assert result.value("avg_daily_volume") == pytest.approx(1_000 * 16 / 20)
    assert result.value("zero_volume_ratio") == pytest.approx(4 / 20)


def test_too_few_observations_is_insufficient_not_zero():
    days = [s.day for s in SESSIONS]
    bars = [bar(d) for d in days[-10:]]  # only 10 of the 20-session window
    result = compute(bars, [A], SESSIONS)[A]
    adv = result.values["avg_daily_volume"]
    assert adv.status is MetricStatus.INSUFFICIENT_DATA
    assert adv.value is None
    assert adv.details["partial_value"] == pytest.approx(100_000.0)
    assert result.value("history_coverage") == pytest.approx(10 / 60)
    none = compute([], [B], SESSIONS)[B]
    assert none.values["avg_daily_volume"].status is MetricStatus.NO_DATA
    assert none.value("history_coverage") == 0.0


def test_atr_is_the_mean_true_range_with_gaps_respected():
    days = [s.day for s in SESSIONS]
    bars = [bar(d, close=100.0, high=102.0, low=99.0) for d in days]
    result = compute(bars, [A], SESSIONS)[A]
    assert result.value("atr") == pytest.approx(3.0)
    assert result.value("atr_pct") == pytest.approx(0.03)


def test_realized_volatility_uses_consecutive_session_log_returns():
    days = [s.day for s in SESSIONS]
    closes = [100.0 * (1.01 if i % 2 else 0.99) ** (i % 2) for i in range(60)]
    bars = [bar(d, close=c) for d, c in zip(days, closes, strict=True)]
    result = compute(bars, [A], SESSIONS)[A]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, 60)][-20:]
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    assert result.value("realized_volatility") == pytest.approx(std * math.sqrt(252))


def test_momentum_relative_volume_and_relative_strength():
    days = [s.day for s in SESSIONS]
    asset = [
        bar(d, close=100.0 + i, volume=1_000.0 if i < 55 else 2_000.0) for i, d in enumerate(days)
    ]
    bench = [bar(d, close=200.0, asset=B) for d in days]
    result = compute(asset + bench, [A, B], SESSIONS, benchmark=B)
    a = result[A]
    assert a.value("return_20d") == pytest.approx(159.0 / 139.0 - 1)
    assert a.value("relative_volume") == pytest.approx(2_000 / ((15 * 1_000 + 5 * 2_000) / 20))
    assert a.value("relative_strength_20d") == pytest.approx(159.0 / 139.0 - 1)
    assert result[B].value("relative_strength_20d") is None  # the benchmark itself


def test_staleness_counts_completed_sessions_since_the_last_valid_bar():
    days = [s.day for s in SESSIONS]
    result = compute([bar(d) for d in days[:-3]], [A], SESSIONS)[A]
    assert result.value("sessions_since_last_bar") == 3.0


def test_spread_measurements_are_no_data_without_quotes():
    missing = spread_measurements(A, None, 5)
    assert {m.metric: m.status for m in missing} == {
        "median_spread_bps": MetricStatus.NO_DATA,
        "invalid_quote_ratio": MetricStatus.NO_DATA,
    }
    summary = SpreadSummary(1000, 4.2, 0.001, SESSIONS[-5].regular_open, SESSIONS[-1].regular_close)
    present = {m.metric: m for m in spread_measurements(A, summary, 5)}
    assert present["median_spread_bps"].value == 4.2
    assert present["median_spread_bps"].observations == 1000


def test_window_must_match_the_history_length():
    with pytest.raises(ValueError, match="expected sessions"):
        compute([], [A], SESSIONS[:10], windows=WINDOWS)

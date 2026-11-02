"""Feature sets, classifiers, participation and cross-asset statistics (Phase 3B).

All series here are SYNTHETIC test fixtures built in this file (no provider data).
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.feature_engine.features import Bar, FeatureQuality, compute_features, weekly_bars
from app.market_intelligence.classification import UNCERTAIN, ClassificationQuality
from app.market_intelligence.config import (
    DAILY,
    CorrelationConfig,
    IntelligenceConfig,
    ParticipationConfig,
    StabilityConfig,
    TrendConfig,
    VolatilityConfig,
)
from app.market_intelligence.correlation import (
    INSUFFICIENT_OVERLAP,
    cross_asset,
    fisher_interval,
    session_returns,
    top_eigenvalue,
)
from app.market_intelligence.participation import MemberView, participation
from app.market_intelligence.stability import classify_stability, stress_evidence
from app.market_intelligence.trend import BEARISH, BULLISH, SIDEWAYS, classify_trend
from app.market_intelligence.volatility import (
    ELEVATED,
    EXTREME,
    LOW,
    NORMAL,
    class_shares,
    classify_volatility,
)

T0 = datetime(2025, 1, 2, 5, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def bars(closes, *, spread=0.01, start=T0, step=timedelta(days=1)):
    """SYNTHETIC bars: high/low a fixed fraction around the close, opened at the prior close."""
    out = []
    for i, close in enumerate(closes):
        at = start + i * step
        out.append(
            Bar(
                event_time=at,
                open=closes[i - 1] if i else close,
                high=close * (1 + spread),
                low=close * (1 - spread),
                close=close,
                volume=1_000_000.0,
                available_at=at + timedelta(hours=20),
            )
        )
    return out


def wiggle(i: int, size: float = 0.01) -> float:
    """Deterministic noise in [-size, size] (no randomness in tests)."""
    return size * math.sin(i * 1.7) * math.cos(i * 0.31)


def trending(count, drift, noise=0.01, start=100.0):
    return [start * math.exp(drift * i + wiggle(i, noise)) for i in range(count)]


def features(closes, *, expected=None, spread=0.01, benchmark=None):
    series = bars(closes, spread=spread)
    return compute_features(
        series,
        DAILY,
        as_of=AS_OF,
        expected_bars=expected or min(len(series), DAILY.window),
        benchmark=benchmark,
    )


def sums_to_one(classification):
    assert sum(classification.probabilities.values()) == pytest.approx(1.0)
    assert all(p >= 0 for p in classification.probabilities.values())


# ------------------------------------------------------------------ feature sets
def test_feature_set_of_a_steady_uptrend():
    f = features(trending(180, 0.004))
    assert f.quality is FeatureQuality.OK
    assert f.bars_used == DAILY.window  # a fixed window, not the whole history
    assert f.get("close_vs_sma_slow") > 0
    assert f.get("sma_fast_vs_slow") > 0
    assert f.get("slow_slope_per_bar") == pytest.approx(0.004, rel=0.1)
    assert f.get("adx") > 25
    assert f.get("plus_di") > f.get("minus_di")


def test_too_few_bars_give_no_values():
    f = features(trending(60, 0.004), expected=DAILY.window)
    assert f.quality is FeatureQuality.INSUFFICIENT_DATA
    assert f.values == {}
    assert "needed" in f.notes[0]


def test_missing_bars_are_counted_not_filled():
    full = trending(DAILY.window, 0.002)
    gappy = [c for i, c in enumerate(full) if i % 20 != 7]  # 7 bars missing
    f = features(gappy, expected=DAILY.window)
    assert f.quality is FeatureQuality.PARTIAL
    assert f.missing_bars == 7
    assert f.coverage == pytest.approx((DAILY.window - 7) / DAILY.window)
    assert "missing" in f.notes[0]
    many_gaps = [c for i, c in enumerate(full) if i % 5 != 0]  # 20 % missing
    assert features(many_gaps, expected=DAILY.window).quality is FeatureQuality.INSUFFICIENT_DATA


def test_values_are_scale_free():
    """Split-adjusted history is rescaled by the provider: ratios must not change."""
    closes = trending(160, 0.003)
    original = features(closes)
    rescaled = features([c / 4 for c in closes])
    for name, value in original.values.items():
        other = rescaled.values[name]
        if value is None:
            assert other is None
        else:
            assert other == pytest.approx(value, rel=1e-9, abs=1e-12), name


def test_relative_strength_against_the_benchmark():
    closes = [100.0] * 140 + [110.0]
    series = bars(closes)
    bench = {b.event_time: 200.0 for b in series}
    bench[series[-1].event_time] = 210.0
    f = features(closes, benchmark=bench)
    # (110/100) / (210/200) - 1
    assert f.get("relative_strength") == pytest.approx(1.1 / 1.05 - 1)
    missing = dict(bench)
    del missing[series[-1].event_time]
    assert features(closes, benchmark=missing).get("relative_strength") is None


def test_missing_volume_is_not_zero():
    series = bars(trending(160, 0.002))
    series[-3] = Bar(**{**series[-3].__dict__, "volume": None})
    f = compute_features(series, DAILY, as_of=AS_OF, expected_bars=DAILY.window)
    assert f.get("volume_ratio") is None


def test_weekly_bars_aggregate_only_complete_weeks():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(12)]  # Mon 5 .. Fri 16
    days = [d for d in days if d.weekday() < 5]  # 10 sessions, weeks 2 and 3
    daily = bars([100.0 + i for i in range(10)], start=T0)
    complete = {(2026, 2)}
    weekly = weekly_bars(daily, days, complete)
    assert len(weekly) == 1
    week = weekly[0]
    assert week.open == daily[0].open
    assert week.close == daily[4].close
    assert week.high == max(b.high for b in daily[:5])
    assert week.low == min(b.low for b in daily[:5])
    assert week.volume == 5_000_000.0
    assert week.available_at == daily[4].available_at


# ------------------------------------------------------------------ trend
CFG = IntelligenceConfig(timeframes=[DAILY])


def trend_of(closes, previous_closes=None, **kw):
    current = features(closes, **kw)
    previous = None if previous_closes is None else features(previous_closes)
    return classify_trend(current, previous, DAILY, CFG.trend)


def test_trend_classification_bullish_bearish_and_sideways():
    up = trend_of(trending(180, 0.004))
    down = trend_of(trending(180, -0.004))
    flat = trend_of([100 * (1 + wiggle(i, 0.03)) for i in range(180)], spread=0.02)
    assert up.label == BULLISH
    assert down.label == BEARISH
    assert flat.label in (SIDEWAYS, UNCERTAIN)
    assert flat.probabilities[SIDEWAYS] > flat.probabilities[BULLISH]
    assert flat.probabilities[SIDEWAYS] > flat.probabilities[BEARISH]
    for c in (up, down, flat):
        sums_to_one(c)
    assert up.evidence["direction_score"] > 0.5
    assert up.algorithm == "trend-evidence-v1"


def test_a_reversal_moves_probability_to_transitioning():
    rise = trending(170, 0.004)
    reversal = rise + [rise[-1] * math.exp(-0.02 * (i + 1)) for i in range(12)]
    steady = trend_of(trending(182, 0.004), previous_closes=trending(177, 0.004))
    turning = trend_of(reversal, previous_closes=reversal[:-5])
    assert turning.probabilities["TRANSITIONING"] > steady.probabilities["TRANSITIONING"]
    assert turning.evidence["component_spread"] > steady.evidence["component_spread"]


def test_insufficient_history_is_uncertain_with_certainty():
    c = trend_of(trending(40, 0.004), expected=DAILY.window)
    assert c.label == UNCERTAIN
    assert c.probabilities[UNCERTAIN] == 1.0
    assert c.quality is ClassificationQuality.INSUFFICIENT_DATA


def test_missing_bars_raise_uncertainty():
    full = trending(DAILY.window, 0.004)
    gappy = [c for i, c in enumerate(full) if i % 20 != 7]
    complete = trend_of(full)
    partial = trend_of(gappy, expected=DAILY.window)
    assert complete.probabilities[UNCERTAIN] == pytest.approx(0.0)
    assert partial.probabilities[UNCERTAIN] > 0.2
    assert partial.quality is ClassificationQuality.PARTIAL
    sums_to_one(partial)


def test_low_confidence_yields_an_uncertain_label():
    strict = TrendConfig(confidence_floor=0.99)
    c = classify_trend(features(trending(180, 0.004)), None, DAILY, strict)
    assert c.label == UNCERTAIN
    assert "confidence floor" in c.reasons[-1]


# ------------------------------------------------------------------ volatility
def test_volatility_class_shares_follow_the_cut_points():
    cfg = VolatilityConfig()
    for percentile, expected in ((0.1, LOW), (0.5, NORMAL), (0.85, ELEVATED), (0.99, EXTREME)):
        shares = class_shares(percentile, cfg)
        assert max(shares, key=lambda k: shares[k]) == expected
        assert sum(shares.values()) == pytest.approx(1.0)
    at_boundary = class_shares(0.75, cfg)
    # At the 0.75 cut point the elevated share is exactly one half minus the extreme tail.
    tail = 1 / (1 + math.exp(-(0.75 - 0.95) / 0.03))
    assert at_boundary[ELEVATED] == pytest.approx(0.5 - tail, abs=1e-9)
    assert at_boundary[NORMAL] == pytest.approx(0.5, abs=1e-6)


def test_a_volatility_spike_is_extreme_and_a_regime_change():
    calm = [100 * (1 + wiggle(i, 0.002)) for i in range(160)]
    spike = calm[:-10] + [calm[-11] * (1 + (0.05 if i % 2 else -0.05)) for i in range(10)]
    before = features(spike[:-5])
    after = features(spike, spread=0.03)
    c = classify_volatility(after, before, DAILY, VolatilityConfig())
    assert c.label in (ELEVATED, EXTREME)
    assert c.probabilities[EXTREME] > 0.5
    assert c.evidence["range_state"] == "EXPANDING"
    sums_to_one(c)


def test_a_large_move_is_measured_not_rejected():
    closes = trending(160, 0.001)
    closes[-1] = closes[-2] * 1.25  # +25 % in a session: real, not an error
    f = features(closes)
    assert f.quality is FeatureQuality.OK
    assert f.get("return_1") == pytest.approx(0.25)


# ------------------------------------------------------------------ stability
def test_stability_from_stress_evidence():
    cfg = StabilityConfig()
    calm = classify_stability(
        {
            "volatility": 0.0,
            "expansion": 0.0,
            "drawdown": 0.05,
            "correlation": 0.1,
            "participation": 0.0,
        },
        cfg,
    )
    stressed = classify_stability(
        {
            "volatility": 1.0,
            "expansion": 1.0,
            "drawdown": 1.0,
            "correlation": 0.9,
            "participation": 1.0,
        },
        cfg,
    )
    assert calm.label == "STABLE"
    assert stressed.label == "STRESSED"
    sums_to_one(calm)
    partial = classify_stability({"volatility": 0.0, "drawdown": 0.0}, cfg)
    assert partial.probabilities[UNCERTAIN] == pytest.approx(0.5)  # half the weight missing
    assert partial.quality is ClassificationQuality.PARTIAL
    assert classify_stability({"volatility": 0.0}, cfg).label == UNCERTAIN


def test_stress_evidence_mapping():
    evidence = stress_evidence(
        benchmark_volatility=None,
        range_ratio=1.125,
        drawdown=-0.05,
        average_correlation=0.5,
        above_slow_ma=0.35,
        cfg=StabilityConfig(),
        volatility_cfg=VolatilityConfig(),
    )
    assert evidence == pytest.approx(
        {"expansion": 0.5, "drawdown": 0.5, "correlation": 0.5, "participation": 0.5}
    )


# ------------------------------------------------------------------ participation
def member(closes, label=None, *, shift=0):
    series = bars(closes, start=T0 + timedelta(days=shift))
    f = compute_features(series, DAILY, as_of=AS_OF, expected_bars=DAILY.window)
    return MemberView(uuid.uuid4(), "X", f, None, label)


def test_participation_needs_enough_members_and_counts_moves():
    cfg = ParticipationConfig(min_members=3)
    ups = [member(trending(150, 0.003), BULLISH) for _ in range(3)]
    down = member(trending(150, -0.003), BEARISH)
    stale = member(trending(150, 0.003), BULLISH, shift=-1)  # newest bar a day older
    result = participation([*ups, down, stale], 6, cfg, feed="iex", single_venue=True)
    assert result.status == "OK"
    assert result.members_evaluated == 5
    m = result.metrics
    assert m["stale_excluded"] == 1
    assert m["advancers"] + m["decliners"] + m["unchanged"] == 4
    assert m["bullish_share"] == pytest.approx(4 / 5)
    assert m["above_slow_ma"] == pytest.approx(4 / 5)
    assert any("single venue" in text for text in result.limitations)
    assert "not market-wide breadth" in result.limitations[0]
    few = participation(ups[:2], 2, cfg, feed="iex", single_venue=True)
    assert few.status == "INSUFFICIENT_DATA"
    assert few.metrics == {}


# ------------------------------------------------------------------ correlation
SESSIONS = [T0 + timedelta(days=i) for i in range(130)]


def closes_from(returns):
    price, out = 100.0, {SESSIONS[0]: 100.0}
    for at, r in zip(SESSIONS[1:], returns, strict=False):
        price *= math.exp(r)
        out[at] = price
    return out


def test_a_missing_session_never_becomes_a_two_day_return():
    closes = {SESSIONS[0]: 100.0, SESSIONS[2]: 110.0, SESSIONS[3]: 121.0}
    assert session_returns(closes, SESSIONS[:4]) == [None, None, pytest.approx(math.log(1.1))]


def test_correlations_with_known_answers_and_overlap_rules():
    base = [wiggle(i, 0.02) for i in range(129)]
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    gappy = closes_from([2 * r for r in base])
    for at in SESSIONS[70:110]:  # 40 sessions missing: too little overlap
        gappy.pop(at)
    result = cross_asset(
        {a: closes_from(base), b: closes_from([-r for r in base]), c: gappy},
        SESSIONS,
        CorrelationConfig(),
        benchmark=a,
    )
    pairs = {(p.asset_a, p.asset_b): p for p in result.pairs}
    first, second = sorted((b, c), key=str)
    key = (first, second)
    assert pairs[key].status == INSUFFICIENT_OVERLAP
    assert pairs[key].correlation is None  # never a misleading number
    relation = {r.asset_id: r for r in result.benchmark}
    assert relation[b].correlation == pytest.approx(-1.0)
    assert relation[b].beta == pytest.approx(-1.0)
    assert relation[c].status == INSUFFICIENT_OVERLAP


def test_fisher_interval_and_eigenvalues():
    low, high = fisher_interval(0.5, 28, 1.96)  # type: ignore[misc]
    # atanh(0.5) = 0.549306, se = 1/5 -> tanh(0.549306 -/+ 0.392) = 0.15602, 0.73582
    assert low == pytest.approx(0.15602, abs=1e-4)
    assert high == pytest.approx(0.73582, abs=1e-4)
    assert fisher_interval(0.5, 3, 1.96) is None
    # A perfect correlation's interval is [r, r]; it must always contain r.
    assert fisher_interval(1.0, 60, 1.96) == (1.0, 1.0)
    assert fisher_interval(-1.0, 60, 1.96) == (-1.0, -1.0)
    assert top_eigenvalue([[1, 0.6], [0.6, 1]]) == pytest.approx(1.6)
    assert top_eigenvalue([[1, 1, 1], [1, 1, 1], [1, 1, 1]]) == pytest.approx(3.0)


def test_common_exposure_is_high_when_assets_move_together():
    common = [wiggle(i, 0.02) for i in range(129)]
    together = {
        uuid.uuid4(): closes_from([r + wiggle(i + k * 50, 0.002) for i, r in enumerate(common)])
        for k in range(4)
    }
    apart = {
        uuid.uuid4(): closes_from([wiggle(i * (k + 2) + k * 13, 0.02) for i in range(129)])
        for k in range(4)
    }
    cfg = CorrelationConfig()
    high = cross_asset(together, SESSIONS, cfg, benchmark=None)
    low = cross_asset(apart, SESSIONS, cfg, benchmark=None)
    assert high.absorption_ratio is not None
    assert low.absorption_ratio is not None
    assert high.average_correlation is not None
    assert high.absorption_ratio > 0.9
    assert low.absorption_ratio < high.absorption_ratio
    assert high.average_correlation > 0.9
    assert high.absorption_assets == 4

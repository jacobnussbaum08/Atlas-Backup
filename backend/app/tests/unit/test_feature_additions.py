"""Phase 4B feature additions: new indicators and registry definitions.

Inputs are small hand-made series (TEST FIXTURES), so every expected value can be checked
by hand. The benchmark feature uses FIXTURE histories.
"""

from __future__ import annotations

import math

import pytest

from app.feature_engine import indicators as ind
from app.feature_engine.benchmark import Benchmark, compute_benchmark_feature
from app.feature_engine.registry import (
    REGISTRY,
    FeatureDefinition,
    FeatureRegistryError,
    FeatureRequest,
    FeatureSource,
    FeatureStatus,
)
from app.tests.fixtures.strategy_series import (
    ASSET_ID,
    BENCHMARK_ID,
    Candle,
    from_closes,
    history,
    with_last,
)

NEW = {
    "close_location",
    "true_range_ratio",
    "log_slope",
    "log_slope_tstat",
    "realized_volatility_percentile",
    "prior_bollinger_width_percentile",
    "failed_breakout_count",
    "relative_return",
    "period_news_count",
}


def value(name, candles, **params):
    from app.feature_engine.bar_features import compute_bar_feature

    feature = REGISTRY.resolve(FeatureRequest(name, "1Day", params))
    return compute_bar_feature(feature, history(candles))


def test_new_definitions_are_new_names_at_v1():
    names = {d.name for d in REGISTRY.definitions()}
    assert names >= NEW
    for d in REGISTRY.definitions():
        if d.name in NEW:
            assert d.version == "v1"
    # No name has two versions: nothing earlier was redefined.
    assert len(names) == len(REGISTRY.definitions())


def test_close_location():
    assert ind.close_location(110, 100, 107.5) == 0.75
    assert ind.close_location(100, 100, 100) is None  # empty range
    assert ind.close_location(110, 100, 120) is None  # inconsistent bar


def test_failed_breakouts_counts_breakouts_that_closed_back_inside():
    # Level = max of the 2 highs before each period. Period 3 closes at 12 above level 11,
    # period 4 falls back to 9: one failed upside breakout.
    high = [10.0, 11.0, 10.5, 12.5, 10.0, 10.2]
    low = [9.0, 10.0, 9.5, 11.0, 8.5, 9.0]
    close = [9.5, 10.5, 10.0, 12.0, 9.0, 9.5]
    assert ind.failed_breakouts(high, low, close, 2, 3, upside=True) == 1
    assert ind.failed_breakouts(high, low, close, 2, 3, upside=False) == 0
    assert ind.failed_breakouts(high, low, close, 2, 4, upside=True) is None  # too short


def test_true_range_ratio_compares_the_newest_range_with_the_prior_atr():
    candles = [Candle(100, 101, 99, 100) for _ in range(20)]  # true range 2 each
    candles.append(Candle(100, 106, 100, 105))  # true range 6
    result = value("true_range_ratio", candles, length=3)
    assert result.status is FeatureStatus.VALID
    assert result.value == pytest.approx(3.0)


def test_log_slope_and_its_capped_t_statistic():
    closes = [100 * 1.01**i for i in range(30)]
    slope = value("log_slope", from_closes(closes), length=20)
    tstat = value("log_slope_tstat", from_closes(closes), length=20)
    assert slope.value == pytest.approx(math.log(1.01))
    assert tstat.value == 1000.0  # a perfect fit: capped, not infinite
    assert tstat.status is FeatureStatus.VALID


def test_prior_bollinger_width_percentile_ignores_the_newest_period():
    candles = from_closes([100 + (i % 5) for i in range(150)])
    base = value("prior_bollinger_width_percentile", candles, length=20, history=100, offset=1)
    jumped = value(
        "prior_bollinger_width_percentile",
        with_last(candles, close=candles[-1].close * 1.5, high=candles[-1].close * 1.5),
        length=20,
        history=100,
        offset=1,
    )
    assert base.status is FeatureStatus.VALID
    assert base.value == jumped.value
    assert base.inputs["window"] == 20 + 100 - 1 + 1


def test_realized_volatility_percentile_is_a_rank():
    calm = from_closes([100 * (1 + 0.001 * (-1) ** i) for i in range(140)])
    wild = from_closes(
        [c.close for c in calm[:-10]] + [100 * (1 + 0.05 * (-1) ** i) for i in range(10)]
    )
    low = value("realized_volatility_percentile", calm, length=20, history=100)
    high = value("realized_volatility_percentile", wild, length=20, history=100)
    assert 0 <= low.value <= 1
    assert high.value > 0.9


def _relative(asset, bench, *, same=False, drop=()):
    feature = REGISTRY.resolve(
        FeatureRequest("relative_return", "1Day", {"periods": 5, "benchmark": "SPY"})
    )
    mine = history(asset)
    if same:
        bm = Benchmark("SPY", ASSET_ID, mine)
    elif bench is None:
        bm = Benchmark("SPY", None, None, "FIXTURE: unknown benchmark")
    else:
        bm = Benchmark("SPY", BENCHMARK_ID, history(bench, drop=drop))
    return compute_benchmark_feature(feature, mine, bm, ASSET_ID)


def test_relative_return_subtracts_the_benchmark_return():
    asset = from_closes([100, 101, 102, 103, 104, 110])  # +10% over 5
    bench = from_closes([400, 401, 402, 403, 404, 420])  # +5% over 5
    result = _relative(asset, bench)
    assert result.status is FeatureStatus.VALID
    assert result.value == pytest.approx(0.05)
    assert result.inputs["benchmark_is_asset"] is False
    assert result.source is FeatureSource.BENCHMARK


def test_relative_return_never_fills_missing_benchmark_data():
    asset = from_closes([100, 101, 102, 103, 104, 110])
    bench = from_closes([400, 401, 402, 403, 404, 420])
    assert _relative(asset, None).status is FeatureStatus.UNAVAILABLE
    assert _relative(asset, bench, drop=[3]).status is FeatureStatus.MISSING_INPUTS
    same = _relative(asset, None, same=True)
    assert same.value == 0.0
    assert same.inputs["benchmark_is_asset"] is True


def test_benchmark_parameter_must_be_a_ticker():
    with pytest.raises(FeatureRegistryError, match="upper-case ticker"):
        REGISTRY.resolve(FeatureRequest("relative_return", "1Day", {"benchmark": "spy; drop"}))


def test_benchmark_definitions_need_a_pair_calculation():
    template = REGISTRY.get("relative_return")
    with pytest.raises(FeatureRegistryError):
        FeatureDefinition(
            name="broken",
            version="v1",
            source=FeatureSource.BENCHMARK,
            description="x",
            method="x",
            parameters=(),
            timeframes=template.timeframes,
            required_data=(),
            availability="x",
            window=template.window,
        )


def test_period_news_count_is_anchored_to_the_session_close():
    d = REGISTRY.get("period_news_count")
    assert d.source is FeatureSource.NEWS
    assert {p.name for p in d.parameters} == {"sessions", "category", "min_relevance"}
    assert "close[s - sessions]" in d.method
    assert "never read as 'no news'" in d.method

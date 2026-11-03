"""Benchmark-relative features (Phase 4B): the asset's completed bars combined with a
benchmark's completed bars on exactly the same periods.

Pure, like ``app.feature_engine.bar_features``: the caller loads both histories for the
same completed, settled periods and the same knowledge time. A period missing in either
series makes the value ``MISSING_INPUTS``; an unknown or ambiguous benchmark makes it
``UNAVAILABLE``. Nothing is aligned by interpolation.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.feature_engine.bar_features import (
    BarHistory,
    WindowProblem,
    bar_window,
    window_bars,
    window_inputs,
)
from app.feature_engine.features import Bar
from app.feature_engine.registry import (
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ResolvedFeature,
)


@dataclass(frozen=True)
class Benchmark:
    """A benchmark as resolved for one evaluation: its history, or why there is none."""

    symbol: str
    asset_id: uuid.UUID | None
    history: BarHistory | None
    problem: str | None = None


def compute_benchmark_feature(
    feature: ResolvedFeature, history: BarHistory, benchmark: Benchmark, asset_id: uuid.UUID
) -> FeatureValue:
    definition = feature.definition
    if definition.source is not FeatureSource.BENCHMARK or definition.pair_compute is None:
        raise ValueError(f"{definition.name} is not a benchmark feature")
    if feature.timeframe != history.timeframe:
        raise ValueError("the history's timeframe does not match the feature")
    window = feature.window

    def result(
        status: FeatureStatus,
        reason: str | None,
        value: float | None = None,
        bars: Sequence[Bar] = (),
        bench: Sequence[Bar] = (),
    ) -> FeatureValue:
        return FeatureValue(
            key=feature.key,
            name=definition.name,
            version=definition.version,
            source=definition.source,
            timeframe=feature.timeframe,
            params=feature.param_map,
            value=value,
            status=status,
            reason=reason,
            available_at=max((b.available_at for b in (*bars, *bench)), default=None),
            inputs={
                **window_inputs(window, bars),
                "benchmark": benchmark.symbol,
                "benchmark_asset_id": None
                if benchmark.asset_id is None
                else str(benchmark.asset_id),
                "benchmark_is_asset": benchmark.asset_id == asset_id,
                "benchmark_bars_used": len(bench),
            },
        )

    bars = window_bars(history, window)
    if isinstance(bars, WindowProblem):
        return result(bars.status, bars.reason)
    if benchmark.history is None:
        return result(
            FeatureStatus.UNAVAILABLE, benchmark.problem or "benchmark unavailable", None, bars
        )
    bench = window_bars(benchmark.history, window)
    if isinstance(bench, WindowProblem):
        return result(bench.status, f"benchmark {benchmark.symbol}: {bench.reason}", None, bars)
    if [b.event_time for b in bars] != [b.event_time for b in bench]:
        return result(FeatureStatus.MISSING_INPUTS, "benchmark periods do not match", None, bars)
    value = definition.pair_compute(
        bar_window(feature.timeframe, bars),
        bar_window(feature.timeframe, bench),
        feature.param_map,
    )
    if value is None or not math.isfinite(value):
        return result(FeatureStatus.UNDEFINED, "undefined for these inputs", None, bars, bench)
    return result(FeatureStatus.VALID, None, value, bars, bench)


__all__ = ["Benchmark", "compute_benchmark_feature"]

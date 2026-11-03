"""Bar features from completed periods (Phase 4A).

Pure: the caller (``app.strategies.inputs``) selects the periods that had completed and
settled by the decision time and the bars Atlas knew then; nothing here reads the database
or the clock, so the same inputs always give the same values.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass

from app.feature_engine.features import Bar
from app.feature_engine.registry import (
    BarWindow,
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ResolvedFeature,
)


@dataclass(frozen=True)
class BarHistory:
    """Expected completed periods (oldest first, the newest completed one last) and the
    bars present for them. A period without a bar is missing data."""

    timeframe: str
    periods: Sequence[Hashable]
    bars: Mapping[Hashable, Bar]


def compute_bar_features(
    features: Sequence[ResolvedFeature], history: BarHistory
) -> dict[str, FeatureValue]:
    return {f.key: compute_bar_feature(f, history) for f in features}


def compute_bar_feature(feature: ResolvedFeature, history: BarHistory) -> FeatureValue:
    definition = feature.definition
    if definition.source is not FeatureSource.BARS or definition.compute is None:
        raise ValueError(f"{definition.name} is not a bar feature")
    if feature.timeframe != history.timeframe:
        raise ValueError("the history's timeframe does not match the feature")
    window = feature.window
    periods = list(history.periods[-window:])

    def result(
        status: FeatureStatus,
        reason: str | None,
        value: float | None = None,
        bars: Sequence[Bar] = (),
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
            available_at=max((b.available_at for b in bars), default=None),
            inputs={
                "window": window,
                "bars_used": len(bars),
                "first_event_at": bars[0].event_time.isoformat() if bars else None,
                "last_event_at": bars[-1].event_time.isoformat() if bars else None,
            },
        )

    if len(periods) < window:
        return result(
            FeatureStatus.INSUFFICIENT_DATA,
            f"{len(periods)} completed periods available, {window} needed",
        )
    present = [p for p in periods if p in history.bars]
    if len(present) < window:
        first = next((i for i, p in enumerate(periods) if p in history.bars), None)
        if first is None:
            return result(FeatureStatus.MISSING_INPUTS, "no usable bar in the window")
        if all(p in history.bars for p in periods[first:]):
            # Missing only before the first bar: the stored history starts inside the window.
            return result(
                FeatureStatus.INSUFFICIENT_DATA,
                f"{len(present)} of {window} periods have bars; history starts inside the window",
            )
        missing = len(periods) - len(present)
        return result(
            FeatureStatus.MISSING_INPUTS,
            f"{missing} of {window} periods in the window have no usable bar",
        )
    bars = [history.bars[p] for p in periods]
    data = BarWindow(
        timeframe=feature.timeframe,
        open=[b.open for b in bars],
        high=[b.high for b in bars],
        low=[b.low for b in bars],
        close=[b.close for b in bars],
        volume=[b.volume for b in bars],
    )
    value = definition.compute(data, feature.param_map)
    if value is None or not math.isfinite(value):
        return result(FeatureStatus.UNDEFINED, "undefined for these inputs", None, bars)
    return result(FeatureStatus.VALID, None, value, bars)


__all__ = ["BarHistory", "compute_bar_feature", "compute_bar_features"]

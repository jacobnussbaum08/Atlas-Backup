"""Breakout (spec 11.4): confirmed closes beyond a consolidation range.

Methodology (logic ``breakout-1``), LONG shown; SHORT mirrors (prior low, closes below):

The **level** is the highest high of the ``channel_length`` completed periods *before* the
newest one (``prior_high``): the newest completed period is compared with a level that was
known when it started, and a period still in progress is never an input.

1. **Confirmation**: close above the level by at least ``confirmation_buffer_atr`` ATR.
2. **Strong close**: close location in the period's range at least ``min_close_location``
   (a period that poked above the level but closed weak is a failed intraperiod breakout).
3. **Relative volume**: the breakout period's volume over its ``volume_long`` mean at least
   ``min_volume_ratio``.
4. **Consolidation**: the prior range (prior high - prior low) at most ``max_range_atr`` ATR.
5. **Volatility compression**: the Bollinger band-width percentile as it stood one period
   before the breakout at most ``max_width_percentile``.
6. **Failed breakouts**: at most ``max_recent_failed`` breakouts beyond the same kind of
   level within the prior ``failed_lookback`` periods closed back inside the range.
7. **Overhead resistance** (context): distance to the longer ``long_channel_length``
   prior high, when available.

Levels: entry = close; invalidation = level - ``failed_breakout_atr`` x ATR (back inside the
range: the breakout failed); target = level + ``target_range_multiple`` x range height
(measured move), which must lie beyond the entry.

Strength (uncalibrated): mean of the breakout distance in ATR, the volume ratio against
twice its threshold, the close location and the compression (1 - width percentile).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, model_validator

from app.database.models import AssetClass
from app.feature_engine.registry import FeatureRequest
from app.strategies.base import (
    EvaluationContext,
    Strategy,
    StrategyOutcome,
    StrategyParameters,
    typed,
)
from app.strategies.enums import Direction, StrategyFamily
from app.strategies.signals import (
    Assessment,
    conclude,
    draft,
    eligible_directions,
    fmt,
    invalid_data,
    make_levels,
    number,
    optional_number,
    sign,
    strength,
)


class BreakoutParameters(StrategyParameters):
    holding_horizon_periods: int = Field(
        10, ge=1, le=260, description="expected holding horizon, in periods"
    )
    channel_length: int = Field(20, ge=5, le=260, description="periods defining the range")
    bollinger_length: int = Field(20, ge=5, le=100)
    contraction_history: int = Field(100, ge=20, le=500)
    max_width_percentile: Decimal = Field(
        Decimal("0.25"), gt=0, le=1, description="band-width percentile counted as contraction"
    )
    volume_long: int = Field(20, ge=2, le=260)
    min_volume_ratio: Decimal = Field(
        Decimal("1.5"), gt=0, le=20, description="breakout-period volume over its average"
    )
    atr_length: int = Field(14, ge=2, le=50)
    confirmation_buffer_atr: Decimal = Field(
        Decimal("0.25"), ge=0, le=5, description="close beyond the level by this many ATR"
    )
    failed_breakout_atr: Decimal = Field(
        Decimal("1.0"), gt=0, le=10, description="back inside the range by this many ATR"
    )
    min_close_location: Decimal = Field(
        Decimal("0.6"), ge=0, le=1, description="close within the period's range (LONG)"
    )
    max_range_atr: Decimal = Field(
        Decimal("8.0"), gt=0, le=50, description="widest prior range counted as consolidation"
    )
    failed_lookback: int = Field(20, ge=1, le=120, description="periods checked for failures")
    max_recent_failed: int = Field(1, ge=0, le=20, description="recent failed breakouts allowed")
    long_channel_length: int = Field(
        120, ge=20, le=260, description="longer-term prior extreme (context)"
    )
    target_range_multiple: Decimal = Field(
        Decimal("1.0"), gt=0, le=10, description="measured-move target, in range heights"
    )

    @model_validator(mode="after")
    def _ordered(self) -> BreakoutParameters:
        if self.long_channel_length <= self.channel_length:
            raise ValueError("long_channel_length must exceed channel_length")
        return self


class BreakoutStrategy(Strategy):
    strategy_id = "breakout"
    family = StrategyFamily.BREAKOUT
    name = "Breakout"
    description = (
        "Looks for confirmed, strong closes beyond a consolidation range after volatility "
        "compression, with above-average volume and few recent failed breakouts; a close "
        "back inside the range invalidates."
    )
    logic_version = "breakout-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = BreakoutParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, BreakoutParameters)
        failed = {"length": p.channel_length, "lookback": p.failed_lookback}
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("close_location", timeframe),
            FeatureRequest("prior_high", timeframe, {"length": p.channel_length}),
            FeatureRequest("prior_low", timeframe, {"length": p.channel_length}),
            FeatureRequest(
                "prior_bollinger_width_percentile",
                timeframe,
                {"length": p.bollinger_length, "history": p.contraction_history, "offset": 1},
            ),
            FeatureRequest("volume_ratio", timeframe, {"short": 1, "long": p.volume_long}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("failed_breakout_count", timeframe, {**failed, "side": "HIGH"}),
            FeatureRequest("failed_breakout_count", timeframe, {**failed, "side": "LOW"}),
            FeatureRequest(
                "prior_high", timeframe, {"length": p.long_channel_length}, optional=True
            ),
            FeatureRequest(
                "prior_low", timeframe, {"length": p.long_channel_length}, optional=True
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, BreakoutParameters)
        close_f = ctx.feature("close")
        loc_f = ctx.feature("close_location")
        high_f = ctx.feature("prior_high", length=p.channel_length)
        low_f = ctx.feature("prior_low", length=p.channel_length)
        width_f = ctx.feature(
            "prior_bollinger_width_percentile",
            length=p.bollinger_length,
            history=p.contraction_history,
            offset=1,
        )
        vol_f = ctx.feature("volume_ratio", short=1, long=p.volume_long)
        atr_f = ctx.feature("atr", length=p.atr_length)
        fail_up_f = ctx.feature(
            "failed_breakout_count",
            length=p.channel_length,
            lookback=p.failed_lookback,
            side="HIGH",
        )
        fail_down_f = ctx.feature(
            "failed_breakout_count", length=p.channel_length, lookback=p.failed_lookback, side="LOW"
        )
        long_high_f = ctx.feature("prior_high", length=p.long_channel_length)
        long_low_f = ctx.feature("prior_low", length=p.long_channel_length)

        close, atr = number(close_f), number(atr_f)
        high, low = number(high_f), number(low_f)
        problem = invalid_data(close=close, atr=atr, prices={"prior low": low}) or (
            f"prior high {fmt(high)} below prior low {fmt(low)}" if high < low else None
        )
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        location, width_pct, volume = number(loc_f), number(width_f), number(vol_f)
        range_height = high - low
        range_atr = range_height / atr

        directions, blocked = eligible_directions(ctx, p)

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            level_f = high_f if s > 0 else low_f
            level = high if s > 0 else low
            beyond = s * (close - level) / atr
            a.require(
                "breakout_confirmation",
                beyond >= float(p.confirmation_buffer_atr) and beyond > 0,
                f"close {fmt(close)} is {fmt(beyond)} ATR {'above' if s > 0 else 'below'} the "
                f"prior {p.channel_length}-period {'high' if s > 0 else 'low'} {fmt(level)} "
                f"(min {p.confirmation_buffer_atr})",
                close_f,
                level_f,
                atr_f,
                role="trigger",
            )
            strong = (
                location >= float(p.min_close_location)
                if s > 0
                else location <= 1 - float(p.min_close_location)
            )
            a.require(
                "strong_close",
                strong,
                f"close location {fmt(location)} in the period's range",
                loc_f,
            )
            a.require(
                "relative_volume",
                volume >= float(p.min_volume_ratio),
                f"volume {fmt(volume)} x its {p.volume_long}-period mean "
                f"(min {p.min_volume_ratio})",
                vol_f,
            )
            a.require(
                "consolidation",
                range_atr <= float(p.max_range_atr),
                f"prior range {fmt(low)}-{fmt(high)} is {fmt(range_atr)} ATR "
                f"(max {p.max_range_atr})",
                high_f,
                low_f,
                atr_f,
            )
            a.require(
                "volatility_compression",
                width_pct <= float(p.max_width_percentile),
                f"band-width percentile before the breakout {fmt(width_pct)} "
                f"(max {p.max_width_percentile})",
                width_f,
            )
            fail_f = fail_up_f if s > 0 else fail_down_f
            failures = int(number(fail_f))
            a.require(
                "failed_breakouts",
                failures <= p.max_recent_failed,
                f"{failures} failed breakout(s) in the prior {p.failed_lookback} periods "
                f"(max {p.max_recent_failed})",
                fail_f,
                role="context",
            )
            far_f = long_high_f if s > 0 else long_low_f
            far = optional_number(far_f)
            if far is None:
                a.inform("overhead_resistance", f"longer-term extreme unavailable ({far_f.reason})")
            elif s * (far - close) > 0:
                a.inform(
                    "overhead_resistance",
                    f"{p.long_channel_length}-period {'high' if s > 0 else 'low'} {fmt(far)} is "
                    f"{fmt(s * (far - close) / atr)} ATR away",
                    far_f,
                )
            else:
                a.inform(
                    "overhead_resistance",
                    f"beyond the {p.long_channel_length}-period extreme {fmt(far)}",
                    far_f,
                )
            return a

        assessments = [assess(d) for d in directions]
        drafts = []
        for a in assessments:
            if not a.passed:
                continue
            s = sign(a.direction)
            level = high if s > 0 else low
            levels = make_levels(
                a.direction,
                entry=close,
                invalidation=level - s * float(p.failed_breakout_atr) * atr,
                target=level + s * float(p.target_range_multiple) * range_height,
                methods={
                    "entry": "newest completed close",
                    "invalidation": f"breakout level -/+ {p.failed_breakout_atr} x "
                    f"ATR{p.atr_length} (back inside the range)",
                    "target": f"breakout level +/- {p.target_range_multiple} x the prior "
                    "range height (measured move)",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="RANGE_BREAKOUT",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            beyond = s * (close - level) / atr
            score = strength(
                beyond / 2,
                volume / (2 * float(p.min_volume_ratio)),
                location if s > 0 else 1 - location,
                1 - width_pct,
            )
            drafts.append(draft(a, levels, score, "Breakout"))
        return conclude(assessments, drafts, checks=blocked)


STRATEGY = BreakoutStrategy()

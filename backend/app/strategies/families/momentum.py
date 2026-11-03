"""Momentum (spec 11.1): sustained, accelerating price moves in either direction.

Methodology (logic ``momentum-1``), evaluated on completed periods for each eligible,
enabled direction (LONG shown; SHORT mirrors every sign):

1. **Rate of change across periods**: the returns over ``acceleration_periods`` (short),
   ``lookback_periods`` (medium) and ``long_lookback_periods`` (long) are all above
   ``min_abs_return``.
2. **Acceleration**: the short return per period exceeds the medium return per period.
3. **Persistence**: at least ``min_persistent_periods`` of the newest
   ``persistence_periods`` single-period returns are positive.
4. **Relative strength**: the medium return minus the benchmark's over the same periods is
   above ``min_relative_strength`` (not applicable to the benchmark itself).
5. **EMA confirmation**: close > EMA(fast) > EMA(trend).
6. **Relative volume**: mean volume of the newest ``volume_short`` periods over the
   ``volume_long`` mean is at least ``min_volume_ratio``.
7. **Not exhausted**: RSI between 50 and ``rsi_long_max`` (SHORT: ``rsi_short_min``-50).

ADX and the market trend regime are recorded as context only.

Levels: entry = newest close; invalidation = entry - ``stop_atr_multiple`` x ATR; target =
entry + ``target_atr_multiple`` x ATR; horizon = ``holding_horizon_periods``.

Strength (uncalibrated): mean of the medium move in volatility units, the volume ratio
against twice its threshold, the persistence fraction and the relative strength. The
defaults are conventional starting points, not optimized or validated values.
"""

from __future__ import annotations

import math
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
    clamp,
    conclude,
    draft,
    eligible_directions,
    fmt,
    invalid_data,
    make_levels,
    number,
    optional_label,
    optional_number,
    pct,
    sign,
    strength,
)


class MomentumParameters(StrategyParameters):
    holding_horizon_periods: int = Field(
        10, ge=1, le=260, description="expected holding horizon, in periods"
    )
    lookback_periods: int = Field(20, ge=2, le=260, description="medium momentum return periods")
    acceleration_periods: int = Field(
        5, ge=1, le=60, description="short return compared with the momentum return"
    )
    long_lookback_periods: int = Field(
        60, ge=3, le=260, description="long return that must agree in direction"
    )
    min_abs_return: Decimal = Field(
        Decimal("0"), ge=0, le=1, description="each return must exceed this (fraction)"
    )
    persistence_periods: int = Field(
        3, ge=1, le=20, description="recent periods whose returns are checked for agreement"
    )
    min_persistent_periods: int = Field(
        2, ge=1, le=20, description="of those, how many must move in the signal's direction"
    )
    rsi_length: int = Field(14, ge=2, le=100)
    rsi_long_max: Decimal = Field(
        Decimal("80"), gt=50, le=100, description="LONG: RSI above this counts as exhausted"
    )
    rsi_short_min: Decimal = Field(
        Decimal("20"), ge=0, lt=50, description="SHORT: RSI below this counts as exhausted"
    )
    fast_ema_length: int = Field(20, ge=2, le=200, description="fast confirmation EMA")
    trend_ema_length: int = Field(50, ge=5, le=200, description="trend-alignment EMA")
    volume_short: int = Field(5, ge=1, le=60)
    volume_long: int = Field(20, ge=2, le=260)
    min_volume_ratio: Decimal = Field(
        Decimal("1.2"), gt=0, le=10, description="volume confirmation threshold"
    )
    benchmark_symbol: str = Field(
        "SPY", pattern=r"^[A-Z][A-Z0-9.]{0,9}$", description="relative-strength benchmark"
    )
    min_relative_strength: Decimal = Field(
        Decimal("0"), ge=0, le=1, description="excess return over the benchmark (fraction)"
    )
    require_relative_strength: bool = Field(
        True, description="False: relative strength is recorded as context only"
    )
    atr_length: int = Field(14, ge=2, le=50)
    stop_atr_multiple: Decimal = Field(Decimal("2.0"), gt=0, le=20)
    target_atr_multiple: Decimal = Field(Decimal("3.0"), gt=0, le=40)

    @model_validator(mode="after")
    def _ordered(self) -> MomentumParameters:
        if self.acceleration_periods >= self.lookback_periods:
            raise ValueError("acceleration_periods must be shorter than lookback_periods")
        if self.lookback_periods >= self.long_lookback_periods:
            raise ValueError("lookback_periods must be shorter than long_lookback_periods")
        if self.volume_short > self.volume_long:
            raise ValueError("volume_short must not exceed volume_long")
        if self.fast_ema_length >= self.trend_ema_length:
            raise ValueError("fast_ema_length must be shorter than trend_ema_length")
        if self.min_persistent_periods > self.persistence_periods:
            raise ValueError("min_persistent_periods must not exceed persistence_periods")
        return self


class MomentumStrategy(Strategy):
    strategy_id = "momentum"
    family = StrategyFamily.MOMENTUM
    name = "Momentum"
    description = (
        "Looks for accelerating, persistent price moves across short, medium and long "
        "returns, confirmed by EMAs, relative strength against a benchmark and relative "
        "volume, in either direction."
    )
    logic_version = "momentum-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = MomentumParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, MomentumParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("return", timeframe, {"periods": p.lookback_periods}),
            FeatureRequest("return", timeframe, {"periods": p.acceleration_periods}),
            FeatureRequest("return", timeframe, {"periods": p.long_lookback_periods}),
            # Persistence: each of the newest periods' own return.
            *(
                FeatureRequest("return", timeframe, {"periods": 1, "offset": lag})
                for lag in range(p.persistence_periods)
            ),
            FeatureRequest(
                "relative_return",
                timeframe,
                {"periods": p.lookback_periods, "benchmark": p.benchmark_symbol},
                optional=not p.require_relative_strength,
            ),
            FeatureRequest("rsi", timeframe, {"length": p.rsi_length}),
            FeatureRequest("ema", timeframe, {"length": p.fast_ema_length}),
            FeatureRequest("ema", timeframe, {"length": p.trend_ema_length}),
            FeatureRequest(
                "volume_ratio", timeframe, {"short": p.volume_short, "long": p.volume_long}
            ),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("adx", timeframe, {"length": 14}, optional=True),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "TREND", "scope": "MARKET"},
                optional=True,
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, MomentumParameters)
        close_f = ctx.feature("close")
        short_f = ctx.feature("return", periods=p.acceleration_periods)
        mid_f = ctx.feature("return", periods=p.lookback_periods)
        long_f = ctx.feature("return", periods=p.long_lookback_periods)
        recent_f = [
            ctx.feature("return", periods=1, offset=lag) for lag in range(p.persistence_periods)
        ]
        rel_f = ctx.feature(
            "relative_return", periods=p.lookback_periods, benchmark=p.benchmark_symbol
        )
        rsi_f = ctx.feature("rsi", length=p.rsi_length)
        fast_f = ctx.feature("ema", length=p.fast_ema_length)
        trend_f = ctx.feature("ema", length=p.trend_ema_length)
        vol_f = ctx.feature("volume_ratio", short=p.volume_short, long=p.volume_long)
        atr_f = ctx.feature("atr", length=p.atr_length)
        adx_f = ctx.feature("adx", length=14)
        regime_f = ctx.feature("regime_label", dimension="TREND", scope="MARKET")

        close, atr = number(close_f), number(atr_f)
        fast, trend = number(fast_f), number(trend_f)
        problem = invalid_data(close=close, atr=atr, prices={"EMA": min(fast, trend)})
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        r_short, r_mid, r_long = number(short_f), number(mid_f), number(long_f)
        recent = [number(f) for f in recent_f]
        rsi, volume = number(rsi_f), number(vol_f)
        relative = optional_number(rel_f)
        benchmark_is_asset = bool(rel_f.inputs.get("benchmark_is_asset"))
        atr_pct = atr / close
        # Expected size of a random move over the lookback, for scaling (not a forecast).
        scale = atr_pct * math.sqrt(p.lookback_periods)

        directions, blocked = eligible_directions(ctx, p)

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            floor = float(p.min_abs_return)
            a.require(
                "rate_of_change",
                all(s * r > floor for r in (r_short, r_mid, r_long)),
                f"returns {pct(r_short)} ({p.acceleration_periods}p), {pct(r_mid)} "
                f"({p.lookback_periods}p), {pct(r_long)} ({p.long_lookback_periods}p)",
                short_f,
                mid_f,
                long_f,
                role="trigger",
            )
            rate_short = r_short / p.acceleration_periods
            rate_mid = r_mid / p.lookback_periods
            a.require(
                "acceleration",
                s * (rate_short - rate_mid) > 0,
                f"{pct(rate_short)} per period recently vs {pct(rate_mid)} over the lookback",
                short_f,
                mid_f,
            )
            agreeing = sum(1 for r in recent if s * r > 0)
            a.require(
                "persistence",
                agreeing >= p.min_persistent_periods,
                f"{agreeing} of the last {p.persistence_periods} periods moved "
                f"{'up' if s > 0 else 'down'}",
                *recent_f,
            )
            if benchmark_is_asset:
                a.inform("relative_strength", "not applicable: the asset is the benchmark")
            elif relative is None:
                a.inform("relative_strength", f"unavailable ({rel_f.reason})", rel_f)
            elif p.require_relative_strength:
                a.require(
                    "relative_strength",
                    s * relative > float(p.min_relative_strength),
                    f"{pct(relative)} versus {p.benchmark_symbol} over {p.lookback_periods}p",
                    rel_f,
                )
            else:
                a.inform("relative_strength", f"{pct(relative)} versus {p.benchmark_symbol}", rel_f)
            a.require(
                "ema_confirmation",
                s * (close - fast) > 0 and s * (fast - trend) > 0,
                f"close {fmt(close)}, EMA{p.fast_ema_length} {fmt(fast)}, "
                f"EMA{p.trend_ema_length} {fmt(trend)}",
                close_f,
                fast_f,
                trend_f,
            )
            a.require(
                "relative_volume",
                volume >= float(p.min_volume_ratio),
                f"volume ratio {fmt(volume)} (threshold {p.min_volume_ratio})",
                vol_f,
            )
            if s > 0:
                fresh = 50 <= rsi <= float(p.rsi_long_max)
                bounds = f"50-{p.rsi_long_max}"
            else:
                fresh = float(p.rsi_short_min) <= rsi <= 50
                bounds = f"{p.rsi_short_min}-50"
            a.require("not_exhausted", fresh, f"RSI {fmt(rsi)} within {bounds}", rsi_f)
            adx = optional_number(adx_f)
            if adx is not None:
                a.inform("trend_strength", f"ADX {fmt(adx)}", adx_f)
            label = optional_label(regime_f)
            a.inform(
                "market_regime",
                f"market trend regime {label}" if label else "market trend regime unavailable",
                regime_f,
            )
            return a

        assessments = [assess(d) for d in directions]
        drafts = []
        for a in assessments:
            if not a.passed:
                continue
            s = sign(a.direction)
            levels = make_levels(
                a.direction,
                entry=close,
                invalidation=close - s * float(p.stop_atr_multiple) * atr,
                target=close + s * float(p.target_atr_multiple) * atr,
                methods={
                    "entry": "newest completed close",
                    "invalidation": f"entry -/+ {p.stop_atr_multiple} x ATR{p.atr_length}",
                    "target": f"entry +/- {p.target_atr_multiple} x ATR{p.atr_length}",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="MOMENTUM",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            rs_component = (
                0.5 if relative is None or benchmark_is_asset else 0.5 + s * relative / scale / 2
            )
            score = strength(
                abs(r_mid) / scale / 2,
                volume / (2 * float(p.min_volume_ratio)),
                sum(1 for r in recent if s * r > 0) / p.persistence_periods,
                clamp(rs_component),
            )
            drafts.append(draft(a, levels, score, "Momentum"))
        return conclude(assessments, drafts, checks=blocked)


STRATEGY = MomentumStrategy()

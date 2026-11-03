"""Trend following (spec 11.2): entries within established, strong trends.

Methodology (logic ``trend-following-1``), LONG shown; SHORT mirrors every sign:

1. **Moving-average alignment**: EMA(fast) > EMA(slow) > SMA(long) and close > EMA(slow).
2. **Trend strength (ADX/DMI)**: ADX at least ``min_adx`` and +DI > -DI.
3. **Trend slope**: the log-close regression slope over ``slope_length`` is at least
   ``min_slope`` per period with a t-statistic of at least ``min_slope_tstat``. Together
   with ADX this keeps weak or sideways markets from being read as trends.
4. **Not over-extended**: close at most ``max_extension_atr`` ATR beyond EMA(fast).
5. **Setup**: a *pullback* (close within ``pullback_atr_multiple`` ATR of EMA(fast)) or a
   *continuation* (close above the highest high of the prior ``continuation_length``
   periods, which excludes the newest period).
6. **Trend regime** (optional Phase 3B asset regime): with ``respect_trend_regime`` an
   opposite or SIDEWAYS label blocks; an unavailable regime is recorded, not blocking.

Levels: entry = close; invalidation = entry - ``trailing_atr_multiple`` x ATR (the initial
level of an ATR trail); target = entry + ``target_atr_multiple`` x ATR.

Strength (uncalibrated): mean of ADX excess, slope t-statistic against twice its minimum,
and setup quality (pullback depth closeness to the EMA; continuation fixed at 0.5).
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
    clamp,
    conclude,
    draft,
    eligible_directions,
    fmt,
    invalid_data,
    make_levels,
    number,
    optional_label,
    sign,
    strength,
)


class TrendFollowingParameters(StrategyParameters):
    holding_horizon_periods: int = Field(
        20, ge=1, le=260, description="expected holding horizon, in periods"
    )
    fast_ema_length: int = Field(20, ge=2, le=100)
    slow_ema_length: int = Field(50, ge=3, le=200)
    long_sma_length: int = Field(200, ge=20, le=400, description="long-term trend filter")
    adx_length: int = Field(14, ge=2, le=50)
    min_adx: Decimal = Field(Decimal("20"), ge=0, le=100, description="minimum trend strength")
    atr_length: int = Field(14, ge=2, le=50)
    pullback_atr_multiple: Decimal = Field(
        Decimal("1.0"), gt=0, le=10, description="pullback depth towards the fast EMA, in ATR"
    )
    trailing_atr_multiple: Decimal = Field(
        Decimal("3.0"), gt=0, le=20, description="trailing invalidation distance, in ATR"
    )
    slope_length: int = Field(20, ge=3, le=260, description="closes in the trend-slope fit")
    min_slope: Decimal = Field(
        Decimal("0.0005"), ge=0, le=1, description="minimum log slope per period"
    )
    min_slope_tstat: Decimal = Field(
        Decimal("2.0"), ge=0, le=100, description="minimum slope t-statistic"
    )
    max_extension_atr: Decimal = Field(
        Decimal("3.0"), gt=0, le=20, description="no entries this far beyond the fast EMA"
    )
    continuation_length: int = Field(
        20, ge=2, le=260, description="prior periods whose extreme a continuation must exceed"
    )
    target_atr_multiple: Decimal = Field(Decimal("4.0"), gt=0, le=40)
    respect_trend_regime: bool = Field(
        True, description="block on an opposite or SIDEWAYS Phase 3B asset trend regime"
    )

    @model_validator(mode="after")
    def _ordered(self) -> TrendFollowingParameters:
        if not self.fast_ema_length < self.slow_ema_length < self.long_sma_length:
            raise ValueError("fast_ema_length < slow_ema_length < long_sma_length is required")
        return self


class TrendFollowingStrategy(Strategy):
    strategy_id = "trend_following"
    family = StrategyFamily.TREND_FOLLOWING
    name = "Trend following"
    description = (
        "Follows established trends: aligned moving averages, sufficient ADX with the "
        "matching directional index and a clear regression slope; entries on pullbacks or "
        "continuations, with ATR-based trailing invalidation."
    )
    logic_version = "trend-following-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = TrendFollowingParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, TrendFollowingParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("ema", timeframe, {"length": p.fast_ema_length}),
            FeatureRequest("ema", timeframe, {"length": p.slow_ema_length}),
            FeatureRequest("sma", timeframe, {"length": p.long_sma_length}),
            FeatureRequest("adx", timeframe, {"length": p.adx_length}),
            FeatureRequest("plus_di", timeframe, {"length": p.adx_length}),
            FeatureRequest("minus_di", timeframe, {"length": p.adx_length}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("log_slope", timeframe, {"length": p.slope_length}),
            FeatureRequest("log_slope_tstat", timeframe, {"length": p.slope_length}),
            FeatureRequest("prior_high", timeframe, {"length": p.continuation_length}),
            FeatureRequest("prior_low", timeframe, {"length": p.continuation_length}),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "TREND", "scope": "ASSET"},
                optional=True,
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, TrendFollowingParameters)
        close_f = ctx.feature("close")
        fast_f = ctx.feature("ema", length=p.fast_ema_length)
        slow_f = ctx.feature("ema", length=p.slow_ema_length)
        long_f = ctx.feature("sma", length=p.long_sma_length)
        adx_f = ctx.feature("adx", length=p.adx_length)
        plus_f = ctx.feature("plus_di", length=p.adx_length)
        minus_f = ctx.feature("minus_di", length=p.adx_length)
        atr_f = ctx.feature("atr", length=p.atr_length)
        slope_f = ctx.feature("log_slope", length=p.slope_length)
        tstat_f = ctx.feature("log_slope_tstat", length=p.slope_length)
        high_f = ctx.feature("prior_high", length=p.continuation_length)
        low_f = ctx.feature("prior_low", length=p.continuation_length)
        regime_f = ctx.feature("regime_label", dimension="TREND", scope="ASSET")

        close, atr = number(close_f), number(atr_f)
        fast, slow, long = number(fast_f), number(slow_f), number(long_f)
        prior_high, prior_low = number(high_f), number(low_f)
        problem = invalid_data(
            close=close,
            atr=atr,
            prices={"moving average": min(fast, slow, long), "prior low": prior_low},
        ) or (f"prior high {fmt(prior_high)} below prior low" if prior_high < prior_low else None)
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        adx, plus_di, minus_di = number(adx_f), number(plus_f), number(minus_f)
        slope, tstat = number(slope_f), number(tstat_f)
        label = optional_label(regime_f)
        distance = (close - fast) / atr  # in ATR, signed

        directions, blocked = eligible_directions(ctx, p)

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            a.require(
                "ma_alignment",
                s * (fast - slow) > 0 and s * (slow - long) > 0 and s * (close - slow) > 0,
                f"EMA{p.fast_ema_length} {fmt(fast)}, EMA{p.slow_ema_length} {fmt(slow)}, "
                f"SMA{p.long_sma_length} {fmt(long)}, close {fmt(close)}",
                fast_f,
                slow_f,
                long_f,
                role="trigger",
            )
            di_agrees = s * (plus_di - minus_di) > 0
            a.require(
                "adx_dmi",
                adx >= float(p.min_adx) and di_agrees,
                f"ADX {fmt(adx)} (min {p.min_adx}), +DI {fmt(plus_di)}, -DI {fmt(minus_di)}",
                adx_f,
                plus_f,
                minus_f,
            )
            a.require(
                "trend_slope",
                s * slope >= float(p.min_slope) and s * tstat >= float(p.min_slope_tstat),
                f"slope {slope * 100:+.3f}% per period, t {fmt(tstat)} "
                f"(min {p.min_slope}, t {p.min_slope_tstat})",
                slope_f,
                tstat_f,
            )
            a.require(
                "not_overextended",
                s * distance <= float(p.max_extension_atr),
                f"close {fmt(distance)} ATR from EMA{p.fast_ema_length} "
                f"(max {p.max_extension_atr})",
                close_f,
                fast_f,
                atr_f,
            )
            pullback = abs(distance) <= float(p.pullback_atr_multiple)
            level = prior_high if s > 0 else prior_low
            continuation = s * (close - level) > 0
            setup = "PULLBACK" if pullback else "CONTINUATION" if continuation else None
            a.require(
                "setup",
                setup is not None,
                f"pullback within {p.pullback_atr_multiple} ATR of EMA{p.fast_ema_length}: "
                f"{'yes' if pullback else 'no'}; close beyond the prior "
                f"{p.continuation_length}-period {'high' if s > 0 else 'low'} {fmt(level)}: "
                f"{'yes' if continuation else 'no'}",
                *((fast_f, atr_f) if pullback else (high_f if s > 0 else low_f,)),
            )
            if label is None:
                a.inform("trend_regime", "asset trend regime unavailable", regime_f)
            elif p.respect_trend_regime:
                opposite = "BEARISH" if s > 0 else "BULLISH"
                a.require(
                    "trend_regime",
                    label not in (opposite, "SIDEWAYS"),
                    f"Phase 3B asset trend regime {label}",
                    regime_f,
                    role="context",
                )
            else:
                a.inform("trend_regime", f"Phase 3B asset trend regime {label}", regime_f)
            return a

        assessments = [assess(d) for d in directions]
        drafts = []
        for a in assessments:
            if not a.passed:
                continue
            s = sign(a.direction)
            pullback = abs(distance) <= float(p.pullback_atr_multiple)
            levels = make_levels(
                a.direction,
                entry=close,
                invalidation=close - s * float(p.trailing_atr_multiple) * atr,
                target=close + s * float(p.target_atr_multiple) * atr,
                methods={
                    "entry": "newest completed close",
                    "invalidation": f"entry -/+ {p.trailing_atr_multiple} x ATR{p.atr_length} "
                    "(initial level of an ATR trailing stop)",
                    "target": f"entry +/- {p.target_atr_multiple} x ATR{p.atr_length}",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="PULLBACK" if pullback else "CONTINUATION",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            setup_quality = (
                1 - abs(distance) / float(p.pullback_atr_multiple) / 2 if pullback else 0.5
            )
            score = strength(
                0.5 + (adx - float(p.min_adx)) / 40,
                s * tstat / (2 * max(float(p.min_slope_tstat), 1.0)),
                clamp(setup_quality),
            )
            drafts.append(draft(a, levels, score, "Trend following"))
        return conclude(assessments, drafts, checks=blocked)


STRATEGY = TrendFollowingStrategy()

"""Mean reversion (spec 11.3): statistically stretched prices that have started to revert.

Methodology (logic ``mean-reversion-1``), LONG shown; SHORT mirrors every sign:

1. **Stretch**: at least ``min_extreme_signals`` of three measures are extreme: RSI at or
   below ``rsi_oversold``; Bollinger %B at or below 0 (close at or under the lower band);
   z-score at or below ``-min_abs_zscore``. At least one must be a price-deviation measure
   (band or z-score), so **an extreme RSI alone never produces a proposal**.
2. **Distance from the average**: close at least ``min_atr_distance`` ATR below the
   Bollinger middle band.
3. **Reversal confirmation** (``require_reversal_confirmation``): the newest period closed
   up and in the upper part of its range (close location at least
   ``min_reversal_close_location``).
4. **Trend filter**: with ``require_trend_alignment`` LONG needs close above
   SMA(``trend_filter_sma_length``) (buying a dip within a long-term uptrend).
5. **No established opposing trend**: the log-close regression over
   ``trend_slope_length`` periods must not slope against the trade with a t-statistic of
   ``max_opposing_slope_tstat`` or more. (ADX is recorded as context only: the stretch
   itself drives a short-term ADX up, so it cannot tell a dip from a downtrend.)
6. **Market regime** (Phase 3B, optional): with ``respect_market_trend`` a BEARISH market
   trend blocks LONG and a BULLISH one blocks SHORT; a market volatility regime listed in
   ``blocked_volatility_regimes`` blocks. An unavailable regime is recorded, not blocking.

Levels: entry = close; target = the Bollinger middle band (the mean reverted to);
invalidation = entry - ``stop_atr_multiple`` x ATR.

Strength (uncalibrated): mean of the share of extreme measures, the stretch in ATR beyond
the minimum, and the reversal close location.
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
    optional_label,
    pct,
    sign,
    strength,
)

VOLATILITY_LABELS = ("LOW", "NORMAL", "ELEVATED", "EXTREME")


class MeanReversionParameters(StrategyParameters):
    rsi_length: int = Field(14, ge=2, le=100)
    rsi_oversold: Decimal = Field(Decimal("30"), gt=0, lt=50)
    rsi_overbought: Decimal = Field(Decimal("70"), gt=50, lt=100)
    bollinger_length: int = Field(20, ge=5, le=100)
    bollinger_k: Decimal = Field(Decimal("2.0"), ge=Decimal("0.5"), le=4)
    zscore_length: int = Field(20, ge=5, le=100)
    min_abs_zscore: Decimal = Field(
        Decimal("2.0"), gt=0, le=6, description="minimum statistical deviation"
    )
    atr_length: int = Field(14, ge=2, le=50)
    trend_filter_sma_length: int = Field(
        200, ge=20, le=400, description="no counter-trend proposals against this average"
    )
    require_reversal_confirmation: bool = Field(
        True, description="the newest period must already move back towards the mean"
    )
    min_extreme_signals: int = Field(
        2, ge=1, le=3, description="of RSI, band and z-score; one must be band or z-score"
    )
    min_atr_distance: Decimal = Field(
        Decimal("1.0"), ge=0, le=20, description="minimum distance from the middle band, in ATR"
    )
    min_reversal_close_location: Decimal = Field(
        Decimal("0.5"), ge=0, le=1, description="reversal period's close within its range"
    )
    require_trend_alignment: bool = Field(
        True, description="LONG only above, SHORT only below the trend-filter SMA"
    )
    adx_length: int = Field(14, ge=2, le=50, description="ADX recorded as context")
    trend_slope_length: int = Field(
        50, ge=10, le=260, description="periods of the established-trend regression"
    )
    max_opposing_slope_tstat: Decimal = Field(
        Decimal("3.0"),
        gt=0,
        le=1000,
        description="an opposing regression slope this clear (t-statistic) blocks",
    )
    respect_market_trend: bool = Field(
        True, description="no LONG in a BEARISH, no SHORT in a BULLISH Phase 3B market"
    )
    blocked_volatility_regimes: tuple[str, ...] = Field(
        ("EXTREME",), max_length=4, description="Phase 3B market volatility labels that block"
    )
    stop_atr_multiple: Decimal = Field(Decimal("1.5"), gt=0, le=20)

    @model_validator(mode="after")
    def _ordered(self) -> MeanReversionParameters:
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError("rsi_oversold must be below rsi_overbought")
        unknown = [r for r in self.blocked_volatility_regimes if r not in VOLATILITY_LABELS]
        if unknown:
            raise ValueError(f"unknown volatility regime(s): {', '.join(unknown)}")
        return self


class MeanReversionStrategy(Strategy):
    strategy_id = "mean_reversion"
    family = StrategyFamily.MEAN_REVERSION
    name = "Mean reversion"
    description = (
        "Looks for statistically stretched prices (at least two of RSI, Bollinger bands and "
        "z-score, never RSI alone) that have started to revert, filtered by the long-term "
        "trend, established opposing trends and the market trend and volatility regimes."
    )
    logic_version = "mean-reversion-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = MeanReversionParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, MeanReversionParameters)
        bands = {"length": p.bollinger_length, "k": float(p.bollinger_k)}
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("return", timeframe, {"periods": 1}),
            FeatureRequest("close_location", timeframe),
            FeatureRequest("rsi", timeframe, {"length": p.rsi_length}),
            FeatureRequest("bollinger_percent_b", timeframe, bands),
            FeatureRequest("bollinger_middle", timeframe, bands),
            FeatureRequest("bollinger_upper", timeframe, bands),
            FeatureRequest("bollinger_lower", timeframe, bands),
            FeatureRequest("zscore", timeframe, {"length": p.zscore_length}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("sma", timeframe, {"length": p.trend_filter_sma_length}),
            FeatureRequest("adx", timeframe, {"length": p.adx_length}),
            FeatureRequest("plus_di", timeframe, {"length": p.adx_length}),
            FeatureRequest("minus_di", timeframe, {"length": p.adx_length}),
            FeatureRequest("log_slope_tstat", timeframe, {"length": p.trend_slope_length}),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "VOLATILITY", "scope": "MARKET"},
                optional=True,
            ),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "TREND", "scope": "MARKET"},
                optional=True,
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, MeanReversionParameters)
        close_f = ctx.feature("close")
        ret_f = ctx.feature("return", periods=1)
        loc_f = ctx.feature("close_location")
        rsi_f = ctx.feature("rsi", length=p.rsi_length)
        pb_f = ctx.feature("bollinger_percent_b", length=p.bollinger_length, k=float(p.bollinger_k))
        mid_f = ctx.feature("bollinger_middle", length=p.bollinger_length, k=float(p.bollinger_k))
        up_f = ctx.feature("bollinger_upper", length=p.bollinger_length, k=float(p.bollinger_k))
        lo_f = ctx.feature("bollinger_lower", length=p.bollinger_length, k=float(p.bollinger_k))
        z_f = ctx.feature("zscore", length=p.zscore_length)
        atr_f = ctx.feature("atr", length=p.atr_length)
        sma_f = ctx.feature("sma", length=p.trend_filter_sma_length)
        adx_f = ctx.feature("adx", length=p.adx_length)
        plus_f = ctx.feature("plus_di", length=p.adx_length)
        minus_f = ctx.feature("minus_di", length=p.adx_length)
        tstat_f = ctx.feature("log_slope_tstat", length=p.trend_slope_length)
        regime_f = ctx.feature("regime_label", dimension="VOLATILITY", scope="MARKET")
        market_f = ctx.feature("regime_label", dimension="TREND", scope="MARKET")

        close, atr = number(close_f), number(atr_f)
        middle, upper, lower = number(mid_f), number(up_f), number(lo_f)
        sma = number(sma_f)
        problem = invalid_data(
            close=close, atr=atr, prices={"middle band": middle, "trend SMA": sma}
        ) or (f"upper band {fmt(upper)} below lower band {fmt(lower)}" if upper < lower else None)
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        ret, location = number(ret_f), number(loc_f)
        rsi, percent_b, z = number(rsi_f), number(pb_f), number(z_f)
        adx, plus_di, minus_di = number(adx_f), number(plus_f), number(minus_f)
        regime = optional_label(regime_f)
        market = optional_label(market_f)
        tstat = number(tstat_f)
        distance = (close - middle) / atr

        directions, blocked = eligible_directions(ctx, p)

        def extremes(s: int) -> tuple[bool, bool, bool]:
            if s > 0:
                return (rsi <= float(p.rsi_oversold), percent_b <= 0, z <= -float(p.min_abs_zscore))
            return (rsi >= float(p.rsi_overbought), percent_b >= 1, z >= float(p.min_abs_zscore))

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            rsi_x, band_x, z_x = extremes(s)
            count = sum((rsi_x, band_x, z_x))
            a.require(
                "statistical_stretch",
                count >= p.min_extreme_signals and (band_x or z_x),
                f"RSI {fmt(rsi)} extreme: {'yes' if rsi_x else 'no'}; %B {fmt(percent_b)} "
                f"beyond the band: {'yes' if band_x else 'no'}; z-score {fmt(z)} beyond "
                f"{p.min_abs_zscore}: {'yes' if z_x else 'no'} ({count} of 3, "
                f"{p.min_extreme_signals} needed, RSI alone never suffices)",
                *([rsi_f] if rsi_x else []),
                *([pb_f, up_f if s < 0 else lo_f] if band_x else []),
                *([z_f] if z_x else []),
                role="trigger",
            )
            a.require(
                "distance_from_mean",
                -s * distance >= float(p.min_atr_distance),
                f"close {fmt(distance)} ATR from the middle band {fmt(middle)} "
                f"(min {p.min_atr_distance})",
                mid_f,
                atr_f,
            )
            if p.require_reversal_confirmation:
                reverting = s * ret > 0 and (
                    location >= float(p.min_reversal_close_location)
                    if s > 0
                    else location <= 1 - float(p.min_reversal_close_location)
                )
                a.require(
                    "reversal_confirmation",
                    reverting,
                    f"newest period return {pct(ret)}, close location {fmt(location)}",
                    ret_f,
                    loc_f,
                )
            else:
                a.inform("reversal_confirmation", "not required by configuration", ret_f)
            if p.require_trend_alignment:
                a.require(
                    "long_term_trend",
                    s * (close - sma) > 0,
                    f"close {fmt(close)} vs SMA{p.trend_filter_sma_length} {fmt(sma)}",
                    sma_f,
                    role="context",
                )
            else:
                a.inform("long_term_trend", f"SMA{p.trend_filter_sma_length} {fmt(sma)}", sma_f)
            a.require(
                "opposing_trend",
                -s * tstat < float(p.max_opposing_slope_tstat),
                f"{p.trend_slope_length}-period slope t-statistic {fmt(tstat)} "
                f"(blocks at {'-' if s > 0 else '+'}{p.max_opposing_slope_tstat})",
                tstat_f,
                role="context",
            )
            a.inform(
                "trend_strength",
                f"ADX {fmt(adx)}, +DI {fmt(plus_di)}, -DI {fmt(minus_di)} (context only)",
                adx_f,
                plus_f,
                minus_f,
            )
            if market is None:
                a.inform("market_trend", "market trend regime unavailable", market_f)
            elif p.respect_market_trend:
                a.require(
                    "market_trend",
                    market != ("BEARISH" if s > 0 else "BULLISH"),
                    f"Phase 3B market trend regime {market}",
                    market_f,
                    role="context",
                )
            else:
                a.inform("market_trend", f"Phase 3B market trend regime {market}", market_f)
            if regime is None:
                a.inform("volatility_regime", "market volatility regime unavailable", regime_f)
            else:
                a.require(
                    "volatility_regime",
                    regime not in p.blocked_volatility_regimes,
                    f"Phase 3B market volatility regime {regime}",
                    regime_f,
                    role="context",
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
                target=middle,
                methods={
                    "entry": "newest completed close",
                    "invalidation": f"entry -/+ {p.stop_atr_multiple} x ATR{p.atr_length}",
                    "target": f"Bollinger middle band (SMA{p.bollinger_length})",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="REVERSION_TO_MEAN",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            score = strength(
                sum(extremes(s)) / 3,
                (-s * distance - float(p.min_atr_distance)) / 2 + 0.5,
                location if s > 0 else 1 - location,
            )
            drafts.append(draft(a, levels, score, "Mean reversion"))
        return conclude(assessments, drafts, checks=blocked)


STRATEGY = MeanReversionStrategy()

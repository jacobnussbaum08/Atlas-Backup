"""Volatility (spec 11.5): directional moves out of volatility compression.

High volatility alone is never read as a direction. The setup is a *regime change*:
volatility was compressed, the newest period expanded the range, and price itself says
which way. Methodology (logic ``volatility-1``), LONG shown; SHORT mirrors every sign:

1. **Prior compression**: the Bollinger band-width percentile one period before the newest
   at most ``max_prior_width_percentile``.
2. **Range expansion**: the newest period's true range at least ``expansion_ratio`` times
   the ATR(``atr_long``) of the periods before it.
3. **Regime shift**: ATR(``atr_short``) / ATR(``atr_long``) above ``compression_ratio``
   (the short-term range is no longer contracting).
4. **Not already extreme**: the realized-volatility percentile at most
   ``max_volatility_percentile``.
5. **Directional confirmation**: the newest period moved at least ``min_move_atr`` ATR up
   and closed in the upper part of its range (close location at least
   ``min_close_location``).
6. **Asset volatility regime** (optional Phase 3B label): a label in
   ``blocked_volatility_regimes`` blocks; unavailable is recorded, not blocking.

Levels: entry = close; invalidation = entry - ``stop_atr_multiple`` x ATR(long); target =
entry + ``target_atr_multiple`` x ATR(long).

Strength (uncalibrated): mean of the expansion beyond its threshold, the compression
(1 - prior width percentile), the move in ATR and the close location.
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


class VolatilityParameters(StrategyParameters):
    atr_short: int = Field(5, ge=1, le=50)
    atr_long: int = Field(20, ge=2, le=100)
    expansion_ratio: Decimal = Field(
        Decimal("1.5"),
        gt=1,
        le=10,
        description="newest true range over the prior ATR(atr_long) counted as expansion",
    )
    compression_ratio: Decimal = Field(
        Decimal("0.75"),
        gt=0,
        lt=1,
        description="ATR(short)/ATR(long) at or below this is still contracting",
    )
    bollinger_length: int = Field(20, ge=5, le=100)
    width_history: int = Field(100, ge=20, le=500)
    volatility_length: int = Field(20, ge=5, le=260)
    stop_atr_multiple: Decimal = Field(
        Decimal("2.0"), gt=0, le=20, description="invalidation distance, in long ATR"
    )
    max_prior_width_percentile: Decimal = Field(
        Decimal("0.3"), gt=0, le=1, description="band-width percentile counted as compressed"
    )
    max_volatility_percentile: Decimal = Field(
        Decimal("0.95"), gt=0, le=1, description="realized-volatility percentile limit"
    )
    min_move_atr: Decimal = Field(
        Decimal("1.0"), gt=0, le=10, description="newest period's move, in prior long ATR"
    )
    min_close_location: Decimal = Field(
        Decimal("0.7"), ge=Decimal("0.5"), le=1, description="close within the range (LONG)"
    )
    target_atr_multiple: Decimal = Field(Decimal("3.0"), gt=0, le=40)
    blocked_volatility_regimes: tuple[str, ...] = Field(
        ("EXTREME",), max_length=4, description="Phase 3B asset volatility labels that block"
    )

    @model_validator(mode="after")
    def _ordered(self) -> VolatilityParameters:
        if self.atr_short >= self.atr_long:
            raise ValueError("atr_short must be shorter than atr_long")
        unknown = [r for r in self.blocked_volatility_regimes if r not in VOLATILITY_LABELS]
        if unknown:
            raise ValueError(f"unknown volatility regime(s): {', '.join(unknown)}")
        return self


class VolatilityStrategy(Strategy):
    strategy_id = "volatility"
    family = StrategyFamily.VOLATILITY
    name = "Volatility"
    description = (
        "Reacts to volatility regime changes: range expansion out of compression, with the "
        "direction taken only from the price move itself; high volatility alone is never "
        "a signal. ATR-scaled invalidation."
    )
    logic_version = "volatility-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = VolatilityParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, VolatilityParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("return", timeframe, {"periods": 1}),
            FeatureRequest("close_location", timeframe),
            FeatureRequest("atr_ratio", timeframe, {"short": p.atr_short, "long": p.atr_long}),
            FeatureRequest("atr", timeframe, {"length": p.atr_long}),
            FeatureRequest("true_range_ratio", timeframe, {"length": p.atr_long}),
            FeatureRequest(
                "prior_bollinger_width_percentile",
                timeframe,
                {"length": p.bollinger_length, "history": p.width_history, "offset": 1},
            ),
            FeatureRequest(
                "realized_volatility_percentile",
                timeframe,
                {"length": p.volatility_length, "history": p.width_history},
            ),
            FeatureRequest("realized_volatility", timeframe, {"length": p.volatility_length}),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "VOLATILITY", "scope": "ASSET"},
                optional=True,
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, VolatilityParameters)
        close_f = ctx.feature("close")
        ret_f = ctx.feature("return", periods=1)
        loc_f = ctx.feature("close_location")
        ratio_f = ctx.feature("atr_ratio", short=p.atr_short, long=p.atr_long)
        atr_f = ctx.feature("atr", length=p.atr_long)
        tr_f = ctx.feature("true_range_ratio", length=p.atr_long)
        width_f = ctx.feature(
            "prior_bollinger_width_percentile",
            length=p.bollinger_length,
            history=p.width_history,
            offset=1,
        )
        rvp_f = ctx.feature(
            "realized_volatility_percentile", length=p.volatility_length, history=p.width_history
        )
        rv_f = ctx.feature("realized_volatility", length=p.volatility_length)
        regime_f = ctx.feature("regime_label", dimension="VOLATILITY", scope="ASSET")

        close, atr = number(close_f), number(atr_f)
        problem = invalid_data(close=close, atr=atr)
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        ret, location = number(ret_f), number(loc_f)
        atr_ratio, tr_ratio = number(ratio_f), number(tr_f)
        width_pct, rv_pct, rv = number(width_f), number(rvp_f), number(rv_f)
        regime = optional_label(regime_f)
        previous = close / (1 + ret)
        move_atr = (close - previous) / atr

        directions, blocked = eligible_directions(ctx, p)

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            a.require(
                "prior_compression",
                width_pct <= float(p.max_prior_width_percentile),
                f"band-width percentile one period earlier {fmt(width_pct)} "
                f"(max {p.max_prior_width_percentile})",
                width_f,
                role="context",
            )
            a.require(
                "range_expansion",
                tr_ratio >= float(p.expansion_ratio),
                f"newest true range {fmt(tr_ratio)} x the prior ATR{p.atr_long} "
                f"(min {p.expansion_ratio})",
                tr_f,
                role="trigger",
            )
            a.require(
                "regime_shift",
                atr_ratio > float(p.compression_ratio),
                f"ATR{p.atr_short}/ATR{p.atr_long} {fmt(atr_ratio)} "
                f"(contracting at or below {p.compression_ratio})",
                ratio_f,
            )
            a.require(
                "not_extreme",
                rv_pct <= float(p.max_volatility_percentile),
                f"realized volatility {pct(rv)} annualized, percentile {fmt(rv_pct)} "
                f"(max {p.max_volatility_percentile})",
                rvp_f,
                rv_f,
                role="context",
            )
            closes_strong = (
                location >= float(p.min_close_location)
                if s > 0
                else location <= 1 - float(p.min_close_location)
            )
            a.require(
                "directional_confirmation",
                s * move_atr >= float(p.min_move_atr) and closes_strong,
                f"newest period moved {fmt(move_atr)} ATR ({pct(ret)}), close location "
                f"{fmt(location)}",
                ret_f,
                loc_f,
                atr_f,
            )
            if regime is None:
                a.inform("volatility_regime", "asset volatility regime unavailable", regime_f)
            else:
                a.require(
                    "volatility_regime",
                    regime not in p.blocked_volatility_regimes,
                    f"Phase 3B asset volatility regime {regime}",
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
                target=close + s * float(p.target_atr_multiple) * atr,
                methods={
                    "entry": "newest completed close",
                    "invalidation": f"entry -/+ {p.stop_atr_multiple} x ATR{p.atr_long}",
                    "target": f"entry +/- {p.target_atr_multiple} x ATR{p.atr_long}",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="VOLATILITY_EXPANSION",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            score = strength(
                (tr_ratio - float(p.expansion_ratio)) / float(p.expansion_ratio) + 0.5,
                1 - width_pct,
                s * move_atr / (2 * float(p.min_move_atr)),
                location if s > 0 else 1 - location,
            )
            drafts.append(draft(a, levels, score, "Volatility"))
        return conclude(assessments, drafts, checks=blocked)


STRATEGY = VolatilityStrategy()

"""Trend following (spec 11.2): EMA alignment, moving-average crossovers, ADX, trend
persistence, pullbacks and ATR trailing exits. Interface and parameters only; logic in
Phase 4B.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, model_validator

from app.database.models import AssetClass
from app.feature_engine.registry import FeatureRequest
from app.strategies.base import InterfaceOnlyStrategy, StrategyParameters, typed
from app.strategies.enums import StrategyFamily


class TrendFollowingParameters(StrategyParameters):
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

    @model_validator(mode="after")
    def _ordered(self) -> TrendFollowingParameters:
        if not self.fast_ema_length < self.slow_ema_length < self.long_sma_length:
            raise ValueError("fast_ema_length < slow_ema_length < long_sma_length is required")
        return self


class TrendFollowingStrategy(InterfaceOnlyStrategy):
    strategy_id = "trend_following"
    family = StrategyFamily.TREND_FOLLOWING
    name = "Trend following"
    description = (
        "Follows established trends: aligned moving averages with sufficient ADX, entries "
        "on pullbacks and ATR-based trailing invalidation."
    )
    logic_version = "trend-following-interface-1"
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
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "TREND", "scope": "ASSET"},
                optional=True,
            ),
        ]


STRATEGY = TrendFollowingStrategy()

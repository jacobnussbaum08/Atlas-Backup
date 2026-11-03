"""Mean reversion (spec 11.3): RSI extremes, Bollinger bands, statistical deviation,
volatility-adjusted distance from the average and reversal confirmation. It must not buy
oversold or short overbought assets blindly: a long-term trend filter and a confirmation
requirement are part of its parameters. Interface and parameters only; logic in Phase 4B.
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

    @model_validator(mode="after")
    def _ordered(self) -> MeanReversionParameters:
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError("rsi_oversold must be below rsi_overbought")
        return self


class MeanReversionStrategy(InterfaceOnlyStrategy):
    strategy_id = "mean_reversion"
    family = StrategyFamily.MEAN_REVERSION
    name = "Mean reversion"
    description = (
        "Looks for statistically stretched prices (RSI, Bollinger bands, z-score) that "
        "have started to revert, filtered by the long-term trend."
    )
    logic_version = "mean-reversion-interface-1"
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
            FeatureRequest("rsi", timeframe, {"length": p.rsi_length}),
            FeatureRequest("bollinger_percent_b", timeframe, bands),
            FeatureRequest("bollinger_middle", timeframe, bands),
            FeatureRequest("bollinger_upper", timeframe, bands),
            FeatureRequest("bollinger_lower", timeframe, bands),
            FeatureRequest("zscore", timeframe, {"length": p.zscore_length}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("sma", timeframe, {"length": p.trend_filter_sma_length}),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "VOLATILITY", "scope": "MARKET"},
                optional=True,
            ),
        ]


STRATEGY = MeanReversionStrategy()

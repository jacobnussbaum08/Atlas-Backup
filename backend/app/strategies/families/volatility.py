"""Volatility (spec 11.5): ATR expansion, volatility compression, range expansion, regime
changes and volatility-adjusted invalidation levels. Interface and parameters only; logic
in Phase 4B.
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


class VolatilityParameters(StrategyParameters):
    atr_short: int = Field(5, ge=1, le=50)
    atr_long: int = Field(20, ge=2, le=100)
    expansion_ratio: Decimal = Field(
        Decimal("1.5"), gt=1, le=10, description="short/long ATR counted as expansion"
    )
    compression_ratio: Decimal = Field(
        Decimal("0.75"), gt=0, lt=1, description="short/long ATR counted as compression"
    )
    bollinger_length: int = Field(20, ge=5, le=100)
    width_history: int = Field(100, ge=20, le=500)
    volatility_length: int = Field(20, ge=5, le=260)
    stop_atr_multiple: Decimal = Field(
        Decimal("2.0"), gt=0, le=20, description="invalidation distance, in long ATR"
    )

    @model_validator(mode="after")
    def _ordered(self) -> VolatilityParameters:
        if self.atr_short >= self.atr_long:
            raise ValueError("atr_short must be shorter than atr_long")
        return self


class VolatilityStrategy(InterfaceOnlyStrategy):
    strategy_id = "volatility"
    family = StrategyFamily.VOLATILITY
    name = "Volatility"
    description = (
        "Reacts to changes in volatility: range expansion out of compression and "
        "volatility-regime shifts, with ATR-scaled invalidation."
    )
    logic_version = "volatility-interface-1"
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
            FeatureRequest("atr_ratio", timeframe, {"short": p.atr_short, "long": p.atr_long}),
            FeatureRequest("atr", timeframe, {"length": p.atr_long}),
            FeatureRequest(
                "bollinger_width_percentile",
                timeframe,
                {"length": p.bollinger_length, "history": p.width_history},
            ),
            FeatureRequest("realized_volatility", timeframe, {"length": p.volatility_length}),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "VOLATILITY", "scope": "ASSET"},
                optional=True,
            ),
        ]


STRATEGY = VolatilityStrategy()

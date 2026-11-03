"""Momentum (spec 11.1): price acceleration, momentum persistence, volume confirmation and
trend alignment, long and short. Interface and parameters only; logic in Phase 4B.

Relative strength against a benchmark is not declared yet: it needs a cross-asset feature
(benchmark bars on the same periods) that the registry does not provide in Phase 4A.
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


class MomentumParameters(StrategyParameters):
    lookback_periods: int = Field(20, ge=2, le=260, description="momentum return periods")
    acceleration_periods: int = Field(
        5, ge=1, le=60, description="short return compared with the momentum return"
    )
    persistence_periods: int = Field(
        3, ge=1, le=20, description="recent periods whose returns must agree in sign"
    )
    rsi_length: int = Field(14, ge=2, le=100)
    trend_ema_length: int = Field(50, ge=5, le=200, description="trend-alignment EMA")
    volume_short: int = Field(5, ge=1, le=60)
    volume_long: int = Field(20, ge=2, le=260)
    min_volume_ratio: Decimal = Field(
        Decimal("1.2"), gt=0, le=10, description="volume confirmation threshold"
    )

    @model_validator(mode="after")
    def _ordered(self) -> MomentumParameters:
        if self.acceleration_periods >= self.lookback_periods:
            raise ValueError("acceleration_periods must be shorter than lookback_periods")
        if self.volume_short > self.volume_long:
            raise ValueError("volume_short must not exceed volume_long")
        return self


class MomentumStrategy(InterfaceOnlyStrategy):
    strategy_id = "momentum"
    family = StrategyFamily.MOMENTUM
    name = "Momentum"
    description = (
        "Looks for accelerating, persistent price moves confirmed by volume and aligned "
        "with the prevailing trend, in either direction."
    )
    logic_version = "momentum-interface-1"
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
            # Persistence: each of the newest periods' own return.
            *(
                FeatureRequest("return", timeframe, {"periods": 1, "offset": lag})
                for lag in range(p.persistence_periods)
            ),
            FeatureRequest("rsi", timeframe, {"length": p.rsi_length}),
            FeatureRequest("ema", timeframe, {"length": p.trend_ema_length}),
            FeatureRequest(
                "volume_ratio", timeframe, {"short": p.volume_short, "long": p.volume_long}
            ),
            FeatureRequest("adx", timeframe, {"length": 14}, optional=True),
            FeatureRequest(
                "regime_label",
                timeframe,
                {"dimension": "TREND", "scope": "MARKET"},
                optional=True,
            ),
        ]


STRATEGY = MomentumStrategy()

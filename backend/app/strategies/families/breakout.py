"""Breakout (spec 11.4): support and resistance from prior extremes, consolidation and
volatility contraction, relative volume, breakout confirmation and failed-breakout
invalidation. Interface and parameters only; logic in Phase 4B.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import Field

from app.database.models import AssetClass
from app.feature_engine.registry import FeatureRequest
from app.strategies.base import InterfaceOnlyStrategy, StrategyParameters, typed
from app.strategies.enums import StrategyFamily


class BreakoutParameters(StrategyParameters):
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


class BreakoutStrategy(InterfaceOnlyStrategy):
    strategy_id = "breakout"
    family = StrategyFamily.BREAKOUT
    name = "Breakout"
    description = (
        "Looks for confirmed closes beyond a consolidation range after volatility "
        "contraction, with above-average volume; failed breakouts invalidate."
    )
    logic_version = "breakout-interface-1"
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = BreakoutParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, BreakoutParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("prior_high", timeframe, {"length": p.channel_length}),
            FeatureRequest("prior_low", timeframe, {"length": p.channel_length}),
            FeatureRequest(
                "bollinger_width_percentile",
                timeframe,
                {"length": p.bollinger_length, "history": p.contraction_history},
            ),
            FeatureRequest("volume_ratio", timeframe, {"short": 1, "long": p.volume_long}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
        ]


STRATEGY = BreakoutStrategy()

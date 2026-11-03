"""Event driven (spec 11.6): reacts to Phase 3C news events about an asset.

It consumes the registry's news features, which are UNAVAILABLE whenever Atlas was not
retrieving news for the whole look-back window: a missing article count blocks the
evaluation instead of being read as "no news" (the news counts are required, never
optional). Event categories are Phase 3C keyword estimates, unverified and not
predictive; per the spec the module uses them as context alongside price confirmation and
never trades on an unverified interpretation alone. Economic releases and central-bank
decisions need an economic calendar, which is NOT_CONFIGURED (Phase 3C).

Interface and parameters only; logic in Phase 4B.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, field_validator

from app.database.models import AssetClass
from app.feature_engine.registry import FeatureRequest
from app.news.enums import EventCategory
from app.strategies.base import EvaluationSchedule, InterfaceOnlyStrategy, StrategyParameters, typed
from app.strategies.enums import StrategyFamily, TriggerKind

DEFAULT_CATEGORIES = (
    EventCategory.EARNINGS,
    EventCategory.ANALYST_RATINGS,
    EventCategory.MERGERS_ACQUISITIONS,
    EventCategory.REGULATORY,
)


class EventDrivenParameters(StrategyParameters):
    categories: tuple[EventCategory, ...] = Field(
        DEFAULT_CATEGORIES, min_length=1, max_length=8, description="event categories considered"
    )
    lookback_hours: int = Field(24, ge=1, le=168)
    min_relevance: Decimal = Field(
        Decimal("0.5"), ge=0, le=1, description="minimum relevance-v1 of the article to the asset"
    )
    confirmation_return_periods: int = Field(
        1, ge=1, le=5, description="price move that must confirm the event"
    )
    atr_length: int = Field(14, ge=2, le=50)
    volume_long: int = Field(20, ge=2, le=260)

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, value: tuple[EventCategory, ...]) -> tuple[EventCategory, ...]:
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        return value


class EventDrivenStrategy(InterfaceOnlyStrategy):
    strategy_id = "event_driven"
    family = StrategyFamily.EVENT_DRIVEN
    name = "Event driven"
    description = (
        "Considers company news events (Phase 3C) together with price and volume "
        "confirmation; missing news coverage blocks an evaluation rather than counting as "
        "no news."
    )
    logic_version = "event-driven-interface-1"
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day",)
    schedule = EvaluationSchedule(
        (TriggerKind.BAR_CLOSE, TriggerKind.NEWS_EVENT),
        "after every completed daily period and whenever a news event about the asset "
        "becomes available",
    )
    Parameters: ClassVar[type[StrategyParameters]] = EventDrivenParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, EventDrivenParameters)
        news = {"lookback_hours": p.lookback_hours, "min_relevance": float(p.min_relevance)}
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("return", timeframe, {"periods": p.confirmation_return_periods}),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("volume_ratio", timeframe, {"short": 1, "long": p.volume_long}),
            FeatureRequest("news_event_count", timeframe, {**news, "category": "ANY"}),
            *(
                FeatureRequest("news_event_count", timeframe, {**news, "category": c.value})
                for c in p.categories
            ),
            FeatureRequest(
                "news_hours_since_latest", timeframe, {**news, "category": "ANY"}, optional=True
            ),
        ]


STRATEGY = EventDrivenStrategy()

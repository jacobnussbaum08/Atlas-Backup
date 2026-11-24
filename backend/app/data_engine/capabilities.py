"""Provider capability model.

``ProviderCapabilities`` describes what a provider adapter can deliver: the
intersection of what the provider offers and what the Atlas adapter implements.
``FeedAccess`` describes what the *configured subscription* is entitled to for one
feed. Nothing in Atlas may assume a capability that is not declared here.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.data_engine.calendar import REGULAR_HOURS, FeedHours
from app.data_engine.enums import DataType, FeedCoverage, PriceAdjustment
from app.data_engine.errors import UnsupportedCapabilityError
from app.data_engine.timeframes import Timeframe, TimeframeUnit
from app.database.models.asset import AssetClass

__all__ = [
    "DataType",
    "FeedAccess",
    "FeedCapability",
    "FeedCoverage",
    "PriceAdjustment",
    "ProviderCapabilities",
    "TimeframeRule",
]


class FeedCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    display_name: str
    coverage: FeedCoverage
    description: str
    historical: bool
    streaming: bool
    # Fixed publication delay of the feed itself (e.g. 15 minutes for delayed SIP).
    delay: timedelta = timedelta(0)


class TimeframeRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    unit: TimeframeUnit
    amounts: frozenset[int]


class FeedAccess(BaseModel):
    """Entitlements of the configured subscription for one feed."""

    model_config = ConfigDict(frozen=True)

    provider: str
    feed: str
    coverage: FeedCoverage
    historical: bool
    streaming: bool
    # Historical data newer than this is not available on the plan (e.g. 15 minutes).
    historical_recency_limit: timedelta | None = None
    stream_symbol_limit: int | None = None
    requests_per_minute: int | None = None
    # Local hours in which the feed can publish data on a trading day (2D completeness).
    # Expected windows err wide: a recovery request that finds nothing costs one call,
    # a window that is too narrow would hide real gaps.
    session_hours: FeedHours = REGULAR_HOURS
    notes: tuple[str, ...] = ()

    def require_historical(self) -> None:
        if not self.historical:
            raise UnsupportedCapabilityError(
                f"{self.provider} feed '{self.feed}' does not provide historical data "
                "on the configured subscription"
            )

    def require_streaming(self) -> None:
        if not self.streaming:
            raise UnsupportedCapabilityError(
                f"{self.provider} feed '{self.feed}' cannot be streamed "
                "on the configured subscription"
            )


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    display_name: str
    asset_classes: frozenset[AssetClass]
    historical_data_types: frozenset[DataType]
    streaming_data_types: frozenset[DataType]
    feeds: tuple[FeedCapability, ...]
    historical_timeframes: tuple[TimeframeRule, ...]
    streaming_bar_timeframes: frozenset[Timeframe]
    max_page_size: int | None = Field(default=None, ge=1)
    # Historical price adjustments the provider can apply on request.
    adjustments: frozenset[PriceAdjustment] = frozenset({PriceAdjustment.RAW})
    documentation: tuple[str, ...] = ()
    # Date the declared facts were last checked against the provider's documentation.
    verified_on: date

    def feed(self, code: str) -> FeedCapability:
        for feed in self.feeds:
            if feed.code == code:
                return feed
        known = ", ".join(f.code for f in self.feeds)
        raise UnsupportedCapabilityError(
            f"{self.display_name} has no feed '{code}' (supported: {known})"
        )

    def supports_historical_timeframe(self, timeframe: Timeframe) -> bool:
        return any(
            rule.unit is timeframe.unit and timeframe.amount in rule.amounts
            for rule in self.historical_timeframes
        )

    def require_historical_bars(self, feed: str, timeframe: Timeframe) -> FeedCapability:
        if DataType.BAR not in self.historical_data_types:
            raise UnsupportedCapabilityError(f"{self.display_name} has no historical bars")
        capability = self.feed(feed)
        if not capability.historical:
            raise UnsupportedCapabilityError(
                f"{self.display_name} feed '{feed}' has no historical data"
            )
        if not self.supports_historical_timeframe(timeframe):
            raise UnsupportedCapabilityError(
                f"{self.display_name} does not provide {timeframe} historical bars"
            )
        return capability

    def require_adjustment(self, adjustment: PriceAdjustment) -> None:
        if adjustment not in self.adjustments:
            supported = ", ".join(sorted(a.value for a in self.adjustments))
            raise UnsupportedCapabilityError(
                f"{self.display_name} does not provide {adjustment.value} adjustment "
                f"(supported: {supported})"
            )

    def require_streaming(self, feed: str, data_type: DataType) -> FeedCapability:
        if data_type not in self.streaming_data_types:
            raise UnsupportedCapabilityError(
                f"{self.display_name} does not stream {data_type.value.lower()}s"
            )
        capability = self.feed(feed)
        if not capability.streaming:
            raise UnsupportedCapabilityError(
                f"{self.display_name} feed '{feed}' cannot be streamed"
            )
        return capability

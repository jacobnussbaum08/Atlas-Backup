"""Alpaca stock market-data capabilities and plan entitlements.

Facts below were checked against Alpaca's official documentation on the
``verified_on`` date:

* About the Market Data API: plans, feeds, rate limits, WebSocket symbol limits.
* Historical stock bars reference (``GET /v2/stocks/bars``): timeframes, ``limit``
  (1-10000, across all symbols), feeds ``sip|iex|otc|boats``, default feed ``sip``.
* Real-time stock data and the streaming protocol: ``/v2/{iex|sip|delayed_sip}``,
  channels, message types, error codes.

Atlas only declares feeds it supports for US equities and ETFs: ``iex``, ``sip``
and ``delayed_sip``. BOATS, overnight and OTC feeds are intentionally excluded.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from app.data_engine.calendar import FeedHours
from app.data_engine.capabilities import (
    DataType,
    FeedAccess,
    FeedCapability,
    FeedCoverage,
    PriceAdjustment,
    ProviderCapabilities,
    TimeframeRule,
)
from app.data_engine.enums import StreamChannel
from app.data_engine.errors import UnsupportedCapabilityError
from app.data_engine.providers.alpaca.config import AlpacaDataPlan, AlpacaFeed
from app.data_engine.timeframes import DAY_1, MINUTE_1, TimeframeUnit
from app.database.models.asset import AssetClass

PROVIDER_ID = "alpaca"
DISPLAY_NAME = "Alpaca Markets"

DOCUMENTATION = (
    "https://docs.alpaca.markets/us/docs/about-market-data-api",
    "https://docs.alpaca.markets/reference/stockbars",
    "https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data",
    "https://docs.alpaca.markets/us/docs/streaming-market-data",
)

IEX_SCOPE_NOTE = (
    "IEX is a single exchange: its prices and volumes do not represent consolidated "
    "US market activity"
)

FEEDS: tuple[FeedCapability, ...] = (
    FeedCapability(
        code=AlpacaFeed.IEX.value,
        display_name="IEX",
        coverage=FeedCoverage.SINGLE_VENUE,
        description="Investors Exchange only. Real-time on the free Basic plan.",
        historical=True,
        streaming=True,
    ),
    FeedCapability(
        code=AlpacaFeed.SIP.value,
        display_name="SIP (CTA + UTP)",
        coverage=FeedCoverage.CONSOLIDATED,
        description="Consolidated feed of all US stock exchanges.",
        historical=True,
        streaming=True,
    ),
    FeedCapability(
        code=AlpacaFeed.DELAYED_SIP.value,
        display_name="SIP, 15-minute delay",
        coverage=FeedCoverage.CONSOLIDATED,
        description="Consolidated feed published with a 15-minute delay. Streaming only.",
        historical=False,
        streaming=True,
        delay=timedelta(minutes=15),
    ),
)

ALPACA_CAPABILITIES = ProviderCapabilities(
    provider=PROVIDER_ID,
    display_name=DISPLAY_NAME,
    asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
    # Historical trades and quotes (2D recovery): on IEX the REST history was verified on
    # 2026-10-30 to hold trades the live stream had skipped.
    historical_data_types=frozenset({DataType.BAR, DataType.TRADE, DataType.QUOTE}),
    streaming_data_types=frozenset({DataType.BAR, DataType.QUOTE, DataType.TRADE}),
    feeds=FEEDS,
    historical_timeframes=(
        TimeframeRule(unit=TimeframeUnit.MINUTE, amounts=frozenset(range(1, 60))),
        TimeframeRule(unit=TimeframeUnit.HOUR, amounts=frozenset(range(1, 24))),
        TimeframeRule(unit=TimeframeUnit.DAY, amounts=frozenset({1})),
        TimeframeRule(unit=TimeframeUnit.WEEK, amounts=frozenset({1})),
        TimeframeRule(unit=TimeframeUnit.MONTH, amounts=frozenset({1, 2, 3, 4, 6, 12})),
    ),
    # Stream bars are minute bars ("b") and running daily bars ("d").
    streaming_bar_timeframes=frozenset({MINUTE_1, DAY_1}),
    max_page_size=10_000,
    adjustments=frozenset(PriceAdjustment),  # raw, split, dividend, spin-off, all
    documentation=DOCUMENTATION,
    verified_on=date(2026, 10, 29),
)

# Live channels implemented for Alpaca stocks. Trade corrections and cancel/errors are
# delivered automatically with ``trades`` and are recorded, never applied silently.
# Statuses, LULD and imbalances are not subscribed.
STREAMING_CHANNELS = frozenset(
    {
        StreamChannel.BARS,
        StreamChannel.UPDATED_BARS,
        StreamChannel.DAILY_BARS,
        StreamChannel.TRADES,
        StreamChannel.QUOTES,
    }
)
# Hours in which each feed publishes (America/New_York). IEX: pre-market from 08:00 (IEX
# rule filing, SEC 34-105959) to post-market end 17:00; Atlas's first IEX message on
# 2026-10-30 arrived at 08:00:04. SIP: the consolidated 04:00-20:00 extended day. On
# early-close days the late sessions still run to 17:00 (NYSE), so windows err wide.
FEED_HOURS: dict[AlpacaFeed, FeedHours] = {
    AlpacaFeed.IEX: FeedHours(time(8, 0), time(17, 0), time(17, 0)),
    AlpacaFeed.SIP: FeedHours(time(4, 0), time(20, 0), time(17, 0)),
    AlpacaFeed.DELAYED_SIP: FeedHours(time(4, 0), time(20, 0), time(17, 0)),
}
# Alpaca documents quote sizes ("bs"/"as") in round lots; Atlas stores them unconverted.
QUOTE_SIZE_UNIT = "round_lots"

_PLAN_LIMITS: dict[AlpacaDataPlan, tuple[int, int | None]] = {
    # plan: (historical requests per minute, WebSocket equity symbol limit; None = unlimited)
    AlpacaDataPlan.BASIC: (200, 30),
    AlpacaDataPlan.ALGO_TRADER_PLUS: (10_000, None),
}


def feed_access(feed: AlpacaFeed, plan: AlpacaDataPlan) -> FeedAccess:
    """Entitlements of ``plan`` for ``feed``. Unverified combinations are denied, not assumed."""
    capability = ALPACA_CAPABILITIES.feed(feed.value)
    requests_per_minute, symbol_limit = _PLAN_LIMITS[plan]
    basic = plan is AlpacaDataPlan.BASIC
    notes: list[str] = []
    historical = capability.historical
    streaming = capability.streaming
    recency_limit: timedelta | None = None

    if feed is AlpacaFeed.IEX:
        notes.append(IEX_SCOPE_NOTE)
    elif feed is AlpacaFeed.SIP and basic:
        streaming = False
        recency_limit = timedelta(minutes=15)
        notes.append("Basic plan: no real-time SIP; historical SIP excludes the latest 15 minutes")
    elif feed is AlpacaFeed.DELAYED_SIP:
        streaming = False
        notes.append(
            "Alpaca documents delayed SIP for Broker API plans; access with Trading API "
            "market-data credentials is unverified, so Atlas does not assume it"
        )

    return FeedAccess(
        provider=PROVIDER_ID,
        feed=feed.value,
        coverage=capability.coverage,
        historical=historical,
        streaming=streaming,
        historical_recency_limit=recency_limit,
        stream_symbol_limit=symbol_limit,
        requests_per_minute=requests_per_minute,
        session_hours=FEED_HOURS[feed],
        notes=tuple(notes),
    )


# Alpaca's spelling of each adjustment (query parameter ``adjustment``).
ADJUSTMENT_PARAMS: dict[PriceAdjustment, str] = {
    PriceAdjustment.RAW: "raw",
    PriceAdjustment.SPLIT: "split",
    PriceAdjustment.DIVIDEND: "dividend",
    PriceAdjustment.SPIN_OFF: "spin-off",
    PriceAdjustment.ALL: "all",
}


def parse_feed(code: str) -> AlpacaFeed:
    try:
        return AlpacaFeed(code)
    except ValueError:
        supported = ", ".join(f.value for f in AlpacaFeed)
        raise UnsupportedCapabilityError(
            f"Alpaca feed '{code}' is not supported by Atlas (supported: {supported})"
        ) from None

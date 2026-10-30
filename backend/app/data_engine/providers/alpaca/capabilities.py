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

from datetime import date, timedelta

from app.data_engine.capabilities import (
    DataType,
    FeedAccess,
    FeedCapability,
    FeedCoverage,
    ProviderCapabilities,
    TimeframeRule,
)
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
    # Historical trades/quotes exist at Alpaca but are not implemented by this adapter.
    historical_data_types=frozenset({DataType.BAR}),
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
    documentation=DOCUMENTATION,
    verified_on=date(2026, 10, 29),
)

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
        notes=tuple(notes),
    )


def parse_feed(code: str) -> AlpacaFeed:
    try:
        return AlpacaFeed(code)
    except ValueError:
        supported = ", ".join(f.value for f in AlpacaFeed)
        raise UnsupportedCapabilityError(
            f"Alpaca feed '{code}' is not supported by Atlas (supported: {supported})"
        ) from None

"""Market status and per-asset snapshots for the dashboard (Deliverable 2E).

Everything here is derived from PostgreSQL and the backend calendar; nothing calls the
provider and nothing is estimated:

* **Market status** comes from the NYSE calendar (``calendar``) and the feed's hours.
* **Last price** is the newest VALID trade, or the close of the newest VALID 1-minute
  bar when that is newer. Its source (live stream or provider history) is kept.
* **Previous close** is the close of the stored, VALID daily bar of the trading day
  before the last price's session. It is the provider's daily bar for the configured
  feed; on a single-venue feed (IEX) that is *not* the official consolidated close,
  and the response says so. Without such a bar there is no previous close and no
  change, never a substitute.
* **Freshness** classifies how current the newest observation is, given the feed's
  hours, its fixed delay and the stream's staleness threshold.
* **Monitored assets** are the API's ``MARKET_DATA_SYMBOLS`` plus the symbols the stream
  worker requested, never every mapped asset: since Phase 3A discovery maps the whole
  REDACTED_FOR_GITHUB
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_engine.calendar import (
    FeedHours,
    MarketSession,
    describe_closure,
    is_verified,
    local_date,
    next_session,
    previous_trading_day,
    session_at,
    trading_day,
)
from app.data_engine.capabilities import FeedCoverage
from app.data_engine.enums import (
    UNRESOLVED_GAP_STATUSES,
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.data_engine.schemas import MarketAsset
from app.data_engine.timeframes import US_EQUITY_TIMEZONE
from app.database.models import (
    Asset,
    AssetIdentifier,
    DataQualityEventRecord,
    MarketBarRecord,
    MarketDataGapRecord,
    MarketQuoteRecord,
    MarketTradeRecord,
)
from app.database.repositories import (
    AssetMappingRepository,
    LiveObservationRepository,
    MarketBarRepository,
    SeriesKey,
    StreamRepository,
)

# How far back the newest bar is looked for (covers long weekends and holidays).
LATEST_BAR_LOOKBACK = timedelta(days=10)
PERCENT_QUANTUM = Decimal("0.0001")


class Freshness(StrEnum):
    LIVE = "LIVE"  # streamed, within the staleness threshold, while the feed publishes
    DELAYED = "DELAYED"  # the feed itself is published with a fixed delay
    HISTORICAL = "HISTORICAL"  # newest data was retrieved from provider history (REST)
    STALE = "STALE"  # the feed publishes now, but nothing newer than the threshold
    MARKET_CLOSED = "MARKET_CLOSED"  # outside the feed's hours: last session's data
    NO_DATA = "NO_DATA"


class PriceBasis(StrEnum):
    TRADE = "TRADE"
    BAR_CLOSE = "BAR_CLOSE"


# ------------------------------------------------------------------ market status
class MarketStatus(BaseModel):
    checked_at: datetime
    calendar: str
    calendar_verified: bool = Field(
        description="Whether today's date lies in the years checked against NYSE's calendar"
    )
    session: MarketSession
    is_regular_session: bool
    trading_day: date | None = Field(description="Today's New York date if NYSE trades today")
    closure: str | None = Field(description="Why NYSE is closed today (weekend, holiday)")
    early_close: bool
    regular_open: datetime | None
    regular_close: datetime | None
    next_regular_open: datetime
    next_regular_close: datetime
    feed: str
    feed_open: bool = Field(description="Whether the feed publishes data now")
    feed_window_start: datetime | None
    feed_window_end: datetime | None
    timezone: str


def feed_window(at: datetime, hours: FeedHours) -> tuple[datetime, datetime] | None:
    day = trading_day(local_date(at))
    return None if day is None else hours.window(day)


def feed_open(at: datetime, hours: FeedHours) -> bool:
    window = feed_window(at, hours)
    return window is not None and window[0] <= at < window[1]


def market_status(now: datetime, feed: str, hours: FeedHours) -> MarketStatus:
    today = trading_day(local_date(now))
    session = session_at(now)
    upcoming = next_session(now)
    in_session = session is MarketSession.REGULAR and today is not None
    next_close = today.regular_close if in_session and today is not None else upcoming.regular_close
    window = feed_window(now, hours)
    return MarketStatus(
        checked_at=now,
        calendar="NYSE (rule-based holidays and early closes, plus listed special closures)",
        calendar_verified=is_verified(local_date(now)),
        session=session,
        is_regular_session=session is MarketSession.REGULAR,
        trading_day=today.day if today else None,
        closure=describe_closure(now),
        early_close=bool(today and today.early_close),
        regular_open=today.regular_open if today else None,
        regular_close=today.regular_close if today else None,
        next_regular_open=upcoming.regular_open,
        next_regular_close=next_close,
        feed=feed,
        feed_open=window is not None and window[0] <= now < window[1],
        feed_window_start=window[0] if window else None,
        feed_window_end=window[1] if window else None,
        timezone=str(US_EQUITY_TIMEZONE),
    )


def classify_freshness(
    latest_at: datetime | None,
    source: DataSource | None,
    now: datetime,
    *,
    is_feed_open: bool,
    stale_after: timedelta,
    delay: timedelta = timedelta(0),
) -> Freshness:
    if latest_at is None:
        return Freshness.NO_DATA
    if not is_feed_open:
        return Freshness.MARKET_CLOSED
    if now - latest_at > stale_after + delay:
        return Freshness.STALE
    if source is DataSource.HISTORICAL_REST:
        return Freshness.HISTORICAL
    if delay > timedelta(0):
        return Freshness.DELAYED
    return Freshness.LIVE


def session_midnight(day: date) -> datetime:
    """UTC instant of a daily bar's timestamp (New York midnight of ``day``)."""
    return datetime.combine(day, time(0), tzinfo=US_EQUITY_TIMEZONE).astimezone(UTC)


def percent_change(last: Decimal, previous: Decimal) -> Decimal | None:
    if previous <= 0:
        return None
    return ((last - previous) / previous * 100).quantize(PERCENT_QUANTUM)


# ------------------------------------------------------------------ snapshots
class SnapshotTrade(BaseModel):
    price: Decimal
    size: Decimal | None
    event_time: datetime
    received_at: datetime
    source: DataSource
    quality_status: DataQualityStatus
    exchange: str | None
    conditions: list[str]


class SnapshotQuote(BaseModel):
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    event_time: datetime
    received_at: datetime
    source: DataSource
    quality_status: DataQualityStatus


class SnapshotBar(BaseModel):
    timeframe: str
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    is_complete: bool
    observation_kind: BarObservationKind
    source: DataSource
    revision: int
    quality_status: DataQualityStatus


class LastPrice(BaseModel):
    value: Decimal
    basis: PriceBasis
    event_time: datetime
    source: DataSource


class PreviousClose(BaseModel):
    value: Decimal
    session_date: date
    bar_event_time: datetime
    source: DataSource
    revision: int
    basis: str


class AssetSnapshot(BaseModel):
    asset: MarketAsset
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    feed_delay_seconds: float
    subscribed: bool = Field(description="Confirmed in the live stream's current subscription")
    last_price: LastPrice | None
    previous_close: PreviousClose | None
    previous_close_note: str | None = Field(
        description="Why there is no previous close, or a caveat about the one given"
    )
    change: Decimal | None
    change_percent: Decimal | None
    latest_trade: SnapshotTrade | None
    latest_quote: SnapshotQuote | None
    latest_bar: SnapshotBar | None
    latest_event_at: datetime | None
    freshness: Freshness
    freshness_threshold_seconds: float
    quality_status: DataQualityStatus | None = Field(
        description="Quality status of the observation behind the last price"
    )
    quality_events_24h: dict[str, int] = Field(description="Quality events by severity")
    open_gaps: int = Field(description="Unresolved missing-data intervals for this asset")


@dataclass(frozen=True)
class SnapshotSettings:
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    delay: timedelta
    adjustment: PriceAdjustment
    hours: FeedHours
    stale_after: timedelta


def _trade(row: MarketTradeRecord | None) -> SnapshotTrade | None:
    return None if row is None else SnapshotTrade.model_validate(row, from_attributes=True)


def _quote(row: MarketQuoteRecord | None) -> SnapshotQuote | None:
    return None if row is None else SnapshotQuote.model_validate(row, from_attributes=True)


def _bar(row: MarketBarRecord | None) -> SnapshotBar | None:
    return None if row is None else SnapshotBar.model_validate(row, from_attributes=True)


def build_snapshot(
    session: Session,
    asset: MarketAsset,
    config: SnapshotSettings,
    now: datetime,
    *,
    subscribed: bool,
    quality_events: dict[str, int],
    open_gaps: int,
) -> AssetSnapshot:
    live = LiveObservationRepository(session)
    bars = MarketBarRepository(session)
    valid = [DataQualityStatus.VALID]
    trade = live.latest_trade(asset.asset_id, config.feed, valid)
    quote = live.latest_quote(asset.asset_id, config.feed, valid)
    minute_key = SeriesKey(asset.asset_id, "1Min", config.provider, config.feed, config.adjustment)
    bar = bars.latest_bar(minute_key, statuses=valid, since=now - LATEST_BAR_LOOKBACK)

    last: LastPrice | None = None
    status: DataQualityStatus | None = None
    if trade is not None and (bar is None or trade.event_time >= bar.event_time):
        last = LastPrice(
            value=trade.price,
            basis=PriceBasis.TRADE,
            event_time=trade.event_time,
            source=trade.source,
        )
        status = trade.quality_status
    elif bar is not None:
        last = LastPrice(
            value=bar.close,
            basis=PriceBasis.BAR_CLOSE,
            event_time=bar.event_time,
            source=bar.source,
        )
        status = bar.quality_status

    previous: PreviousClose | None = None
    note: str | None = None
    if last is None:
        note = "no stored trades or 1-minute bars"
    else:
        session_day = local_date(last.event_time)
        prior = previous_trading_day(session_day)
        daily_key = SeriesKey(
            asset.asset_id, "1Day", config.provider, config.feed, config.adjustment
        )
        daily = bars.current_bar(daily_key, session_midnight(prior.day))
        if daily is None:
            note = (
                f"no stored {config.feed} daily bar for {prior.day.isoformat()}; "
                "retrieve 1Day history to show the change"
            )
        elif daily.quality_status is not DataQualityStatus.VALID:
            note = (
                f"the {config.feed} daily bar for {prior.day.isoformat()} is "
                f"{daily.quality_status.value}, so it is not used as a previous close"
            )
        else:
            basis = f"close of the provider's {config.feed} daily bar for {prior.day.isoformat()}"
            if config.feed_coverage is FeedCoverage.SINGLE_VENUE:
                basis += " (single venue: not the official consolidated closing price)"
            previous = PreviousClose(
                value=daily.close,
                session_date=prior.day,
                bar_event_time=daily.event_time,
                source=daily.source,
                revision=daily.revision,
                basis=basis,
            )
            note = basis

    times = [(row.event_time, row.source) for row in (trade, quote, bar) if row is not None]
    latest_at, latest_source = max(times, key=lambda item: item[0]) if times else (None, None)
    freshness = classify_freshness(
        latest_at,
        latest_source,
        now,
        is_feed_open=feed_open(now, config.hours),
        stale_after=config.stale_after,
        delay=config.delay,
    )
    change = None if last is None or previous is None else last.value - previous.value
    return AssetSnapshot(
        asset=asset,
        provider=config.provider,
        feed=config.feed,
        feed_coverage=config.feed_coverage,
        feed_delay_seconds=config.delay.total_seconds(),
        subscribed=subscribed,
        last_price=last,
        previous_close=previous,
        previous_close_note=note,
        change=change,
        change_percent=None
        if last is None or previous is None
        else percent_change(last.value, previous.value),
        latest_trade=_trade(trade),
        latest_quote=_quote(quote),
        latest_bar=_bar(bar),
        latest_event_at=latest_at,
        freshness=freshness,
        freshness_threshold_seconds=config.stale_after.total_seconds(),
        quality_status=status,
        quality_events_24h=quality_events,
        open_gaps=open_gaps,
    )


def quality_event_counts(
    session: Session, provider: str, since: datetime
) -> dict[uuid.UUID, dict[str, int]]:
    rows = session.execute(
        select(DataQualityEventRecord.asset_id, DataQualityEventRecord.severity, func.count())
        .where(
            DataQualityEventRecord.provider == provider,
            DataQualityEventRecord.asset_id.is_not(None),
            DataQualityEventRecord.detected_at >= since,
        )
        .group_by(DataQualityEventRecord.asset_id, DataQualityEventRecord.severity)
    ).all()
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for asset_id, severity, n in rows:
        if asset_id is None:
            continue
        counts.setdefault(asset_id, {})[severity.value] = int(n)
    return counts


def open_gap_counts(session: Session, provider: str, feed: str) -> dict[uuid.UUID, int]:
    rows = session.execute(
        select(MarketDataGapRecord.asset_id, func.count())
        .where(
            MarketDataGapRecord.provider == provider,
            MarketDataGapRecord.feed == feed,
            MarketDataGapRecord.status.in_(list(UNRESOLVED_GAP_STATUSES)),
        )
        .group_by(MarketDataGapRecord.asset_id)
    ).all()
    return {asset_id: int(n) for asset_id, n in rows}


def monitored_assets(
    session: Session, provider: str, feed: str, configured_symbols: Iterable[str]
) -> Sequence[tuple[Asset, AssetIdentifier]]:
    """Mapped assets the dashboard monitors: ``configured_symbols`` (the API process's
    ``MARKET_DATA_SYMBOLS``) plus the provider symbols the stream worker last requested,
    which it may take from its own environment."""
    status = StreamRepository(session).get_status(provider, feed)
    requested = status.requested_subscription if status is not None else {}
    streamed = {s for values in (requested or {}).values() for s in values}
    return AssetMappingRepository(session).current_assets(
        provider, symbols=configured_symbols, provider_symbols=streamed
    )


__all__ = [
    "AssetSnapshot",
    "Freshness",
    "MarketStatus",
    "SnapshotSettings",
    "build_snapshot",
    "classify_freshness",
    "feed_open",
    "market_status",
    "monitored_assets",
    "open_gap_counts",
    "percent_change",
    "quality_event_counts",
    "session_midnight",
]

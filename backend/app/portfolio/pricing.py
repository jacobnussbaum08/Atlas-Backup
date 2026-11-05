"""Valuation prices with provenance and freshness (decision D-133).

For a position at time ``as_of`` the candidate prices are the newest of: the last VALID live
trade, the midpoint of the last VALID live quote with both sides, and the close of the last
complete VALID unadjusted (RAW) bar, each observed and available at or before ``as_of``.
Unadjusted, because ledger quantities are as traded. The newest observation wins (trade,
then quote, then bar on a tie). Nothing is interpolated, carried forward or invented.

Freshness:

* equities and ETFs (exchange calendar XNYS): during the regular session the price must be
  at most ``equity_intraday_max_age_minutes`` old; outside it, it must come from the most
  recent session that has opened (e.g. that session's daily bar);
* crypto: at most ``crypto_max_age_minutes`` old.

A STALE price is reported with its value and provenance, but never posted; a MISSING price
is reported as missing. Either way the position keeps its last posted valuation and the
snapshot is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.config import AccountingSettings
from app.accounting.enums import PriceStatus
from app.asset_discovery.sessions import (
    CALENDARS,
    local_date,
    previous_trading_day,
    session_at,
    trading_day,
)
from app.data_engine.calendar import MarketSession
from app.data_engine.enums import DataQualityStatus, PriceAdjustment
from app.database.models import (
    Asset,
    AssetClass,
    MarketBarRecord,
    MarketQuoteRecord,
    MarketTradeRecord,
)

CALENDAR = CALENDARS["XNYS"]
_PRIORITY = {"TRADE": 0, "QUOTE_MID": 1, "BAR_CLOSE": 2}


@dataclass(frozen=True)
class PriceObservation:
    price: Decimal
    source: str
    observed_at: datetime
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceResult:
    status: PriceStatus
    observation: PriceObservation | None
    reason: str | None = None


def _trade(session: Session, asset_id: Any, as_of: datetime) -> PriceObservation | None:
    row = session.scalar(
        select(MarketTradeRecord)
        .where(
            MarketTradeRecord.asset_id == asset_id,
            MarketTradeRecord.event_time <= as_of,
            MarketTradeRecord.available_at <= as_of,
            MarketTradeRecord.quality_status == DataQualityStatus.VALID,
        )
        .order_by(MarketTradeRecord.event_time.desc(), MarketTradeRecord.id.desc())
        .limit(1)
    )
    if row is None or row.price <= 0:
        return None
    return PriceObservation(
        row.price,
        "TRADE",
        row.event_time,
        {
            "table": "market_trades",
            "id": row.id,
            "provider": row.provider,
            "feed": row.feed,
            "event_time": row.event_time.isoformat(),
            "available_at": row.available_at.isoformat(),
        },
    )


def _quote(session: Session, asset_id: Any, as_of: datetime) -> PriceObservation | None:
    row = session.scalar(
        select(MarketQuoteRecord)
        .where(
            MarketQuoteRecord.asset_id == asset_id,
            MarketQuoteRecord.event_time <= as_of,
            MarketQuoteRecord.available_at <= as_of,
            MarketQuoteRecord.quality_status == DataQualityStatus.VALID,
            MarketQuoteRecord.bid_price > 0,
            MarketQuoteRecord.ask_price >= MarketQuoteRecord.bid_price,
        )
        .order_by(MarketQuoteRecord.event_time.desc(), MarketQuoteRecord.id.desc())
        .limit(1)
    )
    if row is None or row.bid_price is None or row.ask_price is None:
        return None
    mid = (row.bid_price + row.ask_price) / 2
    return PriceObservation(
        mid,
        "QUOTE_MID",
        row.event_time,
        {
            "table": "market_quotes",
            "id": row.id,
            "provider": row.provider,
            "feed": row.feed,
            "bid": str(row.bid_price),
            "ask": str(row.ask_price),
            "event_time": row.event_time.isoformat(),
            "available_at": row.available_at.isoformat(),
        },
    )


def _bar(session: Session, asset_id: Any, as_of: datetime) -> PriceObservation | None:
    row = session.scalar(
        select(MarketBarRecord)
        .where(
            MarketBarRecord.asset_id == asset_id,
            MarketBarRecord.adjustment == PriceAdjustment.RAW,
            MarketBarRecord.is_complete.is_(True),
            MarketBarRecord.quality_status == DataQualityStatus.VALID,
            MarketBarRecord.event_time <= as_of,
            MarketBarRecord.available_at <= as_of,
        )
        .order_by(
            MarketBarRecord.event_time.desc(),
            MarketBarRecord.revision.desc(),
            MarketBarRecord.id.desc(),
        )
        .limit(1)
    )
    if row is None or row.close <= 0:
        return None
    return PriceObservation(
        row.close,
        "BAR_CLOSE",
        row.event_time,
        {
            "table": "market_bars",
            "id": row.id,
            "provider": row.provider,
            "feed": row.feed,
            "timeframe": row.timeframe,
            "revision": row.revision,
            "event_time": row.event_time.isoformat(),
            "available_at": row.available_at.isoformat(),
        },
    )


def freshness(
    asset_class: AssetClass, observed_at: datetime, as_of: datetime, settings: AccountingSettings
) -> tuple[PriceStatus, str | None]:
    if asset_class is AssetClass.CRYPTO:
        limit = timedelta(minutes=settings.crypto_max_age_minutes)
        if as_of - observed_at > limit:
            return PriceStatus.STALE, f"older than {settings.crypto_max_age_minutes} minutes"
        return PriceStatus.FRESH, None
    if asset_class in (AssetClass.EQUITY, AssetClass.ETF):
        if session_at(CALENDAR, as_of) is MarketSession.REGULAR:
            limit = timedelta(minutes=settings.equity_intraday_max_age_minutes)
            if as_of - observed_at > limit:
                return (
                    PriceStatus.STALE,
                    f"older than {settings.equity_intraday_max_age_minutes} minutes during "
                    "the regular session",
                )
            return PriceStatus.FRESH, None
        today = trading_day(CALENDAR, local_date(CALENDAR, as_of))
        latest = (
            today
            if today is not None and today.regular_open <= as_of
            else previous_trading_day(CALENDAR, local_date(CALENDAR, as_of))
        )
        if local_date(CALENDAR, observed_at) < latest.day:
            return PriceStatus.STALE, f"not from the latest session ({latest.day.isoformat()})"
        return PriceStatus.FRESH, None
    return PriceStatus.STALE, f"no valuation rule for {asset_class.value}"


def valuation_price(
    session: Session, asset: Asset, as_of: datetime, settings: AccountingSettings
) -> PriceResult:
    candidates = [
        c
        for c in (
            _trade(session, asset.id, as_of),
            _quote(session, asset.id, as_of),
            _bar(session, asset.id, as_of),
        )
        if c is not None
    ]
    if not candidates:
        return PriceResult(PriceStatus.MISSING, None, "no stored price observation")
    best = sorted(candidates, key=lambda c: (-c.observed_at.timestamp(), _PRIORITY[c.source]))[0]
    status, reason = freshness(asset.asset_class, best.observed_at, as_of, settings)
    return PriceResult(status, best, reason)


__all__ = ["PriceObservation", "PriceResult", "freshness", "valuation_price"]

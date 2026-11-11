"""Market data as the simulated paper broker and the position monitor may see it (Phase 7B).

Only **stored provider observations** are read (``market_quotes``, ``market_trades``,
``market_bars``), only those with quality VALID, and only those that were *available*
(``available_at``) and had *occurred* (``event_time``) at or before the evaluation time.
Nothing is interpolated, carried forward, synthesized or inferred: a missing quote stays
missing and the caller defers.

Cursors make each observation count once: trades and bars after ``(trade_after)`` /
``bar_after``, and a quote other than ``quote_used`` (whose displayed size the order already
used). ``floor`` excludes activity before the order reached the simulator (or before its
stop triggered, or before the position opened): a bar must have *started* at or after it,
so no part of its range predates the order.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_engine.enums import DataQualityStatus, PriceAdjustment
from app.database.models import MarketBarRecord, MarketQuoteRecord, MarketTradeRecord

_TIMEFRAME = re.compile(r"^([1-9][0-9]{0,2})(Min|Hour|Day)$")
_UNITS = {"Min": timedelta(minutes=1), "Hour": timedelta(hours=1), "Day": timedelta(days=1)}
MAX_TRADES = 2000
MAX_BARS = 500


def timeframe_length(timeframe: str) -> timedelta:
    found = _TIMEFRAME.match(timeframe)
    if found is None:
        raise ValueError(f"unsupported bar timeframe {timeframe!r}")
    return int(found.group(1)) * _UNITS[found.group(2)]


@dataclass(frozen=True)
class Quote:
    id: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    event_time: datetime
    available_at: datetime
    provider: str
    feed: str

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def ref(self) -> dict[str, Any]:
        return {
            "table": "market_quotes",
            "id": self.id,
            "provider": self.provider,
            "feed": self.feed,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "bid_size": None if self.bid_size is None else str(self.bid_size),
            "ask_size": None if self.ask_size is None else str(self.ask_size),
            "event_time": self.event_time.isoformat(),
            "available_at": self.available_at.isoformat(),
        }


@dataclass(frozen=True)
class Trade:
    id: int
    price: Decimal
    size: Decimal | None
    event_time: datetime


@dataclass(frozen=True)
class Bar:
    id: int
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    available_at: datetime


@dataclass(frozen=True)
class MarketView:
    """What was observable for one asset at ``as_of`` (since the cursors)."""

    as_of: datetime
    floor: datetime
    quote: Quote | None  # the newest valid two-sided quote at as_of (any age; check freshness)
    trades: tuple[Trade, ...] = ()
    bars: tuple[Bar, ...] = ()
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def newest_event_time(self) -> datetime | None:
        times = [t.event_time for t in self.trades] + [b.end for b in self.bars]
        if self.quote is not None:
            times.append(self.quote.event_time)
        return max(times) if times else None

    def quote_age(self) -> timedelta | None:
        return None if self.quote is None else self.as_of - self.quote.event_time

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "as_of": self.as_of.isoformat(),
            "floor": self.floor.isoformat(),
            "quote": None if self.quote is None else self.quote.ref(),
            "trades": len(self.trades),
            "bars": len(self.bars),
        }
        if self.trades:
            out["trade_ids"] = [self.trades[0].id, self.trades[-1].id]
            out["trade_range"] = [
                str(min(t.price for t in self.trades)),
                str(max(t.price for t in self.trades)),
            ]
        if self.bars:
            out["bar_ids"] = [b.id for b in self.bars][:20]
        if self.truncated:
            out["truncated"] = True
        return out


def _quote(session: Session, asset_id: uuid.UUID, as_of: datetime) -> Quote | None:
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
    return Quote(
        id=row.id,
        bid=row.bid_price,
        ask=row.ask_price,
        bid_size=row.bid_size,
        ask_size=row.ask_size,
        event_time=row.event_time,
        available_at=row.available_at,
        provider=row.provider,
        feed=row.feed,
    )


def load(
    session: Session,
    asset_id: uuid.UUID,
    *,
    as_of: datetime,
    floor: datetime,
    trade_after: tuple[datetime, int] | None = None,
    bar_after: datetime | None = None,
    timeframe: str = "1Min",
) -> MarketView:
    """The view of ``asset_id`` at ``as_of``: the newest quote, and the trades and completed
    bars after the cursors and at or after ``floor``."""
    length = timeframe_length(timeframe)
    trade_query = select(MarketTradeRecord).where(
        MarketTradeRecord.asset_id == asset_id,
        MarketTradeRecord.event_time >= floor,
        MarketTradeRecord.event_time <= as_of,
        MarketTradeRecord.available_at <= as_of,
        MarketTradeRecord.quality_status == DataQualityStatus.VALID,
        MarketTradeRecord.price > 0,
    )
    if trade_after is not None:
        at, last_id = trade_after
        trade_query = trade_query.where(
            (MarketTradeRecord.event_time > at)
            | ((MarketTradeRecord.event_time == at) & (MarketTradeRecord.id > last_id))
        )
    trade_rows = session.scalars(
        trade_query.order_by(MarketTradeRecord.event_time, MarketTradeRecord.id).limit(
            MAX_TRADES + 1
        )
    ).all()
    truncated = len(trade_rows) > MAX_TRADES
    trades = tuple(Trade(r.id, r.price, r.size, r.event_time) for r in trade_rows[:MAX_TRADES])
    # Completed, valid, unadjusted bars that started at/after the floor and ended by as_of.
    bar_query = (
        select(MarketBarRecord)
        .where(
            MarketBarRecord.asset_id == asset_id,
            MarketBarRecord.timeframe == timeframe,
            MarketBarRecord.adjustment == PriceAdjustment.RAW,
            MarketBarRecord.is_complete.is_(True),
            MarketBarRecord.quality_status == DataQualityStatus.VALID,
            MarketBarRecord.event_time >= floor,
            MarketBarRecord.event_time <= as_of - length,
            MarketBarRecord.available_at <= as_of,
        )
        .order_by(MarketBarRecord.event_time, MarketBarRecord.revision.desc())
    )
    if bar_after is not None:
        bar_query = bar_query.where(MarketBarRecord.event_time > bar_after)
    seen: set[datetime] = set()
    bars: list[Bar] = []
    for r in session.scalars(bar_query.limit(MAX_BARS * 4)).all():
        if r.event_time in seen:
            continue  # an older revision of a bar already taken (newest available first)
        seen.add(r.event_time)
        if r.high < r.low or r.low <= 0:
            continue
        bars.append(
            Bar(
                r.id,
                r.event_time,
                r.event_time + length,
                r.open,
                r.high,
                r.low,
                r.close,
                r.volume,
                r.available_at,
            )
        )
        if len(bars) >= MAX_BARS:
            truncated = True
            break
    return MarketView(
        as_of=as_of,
        floor=floor,
        quote=_quote(session, asset_id, as_of),
        trades=trades,
        bars=tuple(bars),
        truncated=truncated,
    )


def has_new_data(
    session: Session, asset_id: uuid.UUID, *, after: datetime | None, as_of: datetime
) -> bool:
    """Whether any observation of the asset became *available* at or after ``after`` (the
    last evaluation time) and by ``as_of``: a cheap pre-check so unchanged orders are
    skipped. Inclusive, because a row available at that instant may have been written just
    after the evaluation read; re-evaluating it is harmless (cursors stop any reuse)."""
    for model in (MarketQuoteRecord, MarketTradeRecord, MarketBarRecord):
        query = select(model.id).where(
            model.asset_id == asset_id, model.event_time <= as_of, model.available_at <= as_of
        )
        if after is not None:
            # event_time precedes available_at; the bound lets the (asset, time) index work.
            query = query.where(
                model.available_at >= after, model.event_time > after - timedelta(days=2)
            )
        if session.scalar(query.limit(1)) is not None:
            return True
    return False


__all__ = [
    "Bar",
    "MarketView",
    "Quote",
    "Trade",
    "has_new_data",
    "load",
    "timeframe_length",
]

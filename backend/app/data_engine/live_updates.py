"""Distribution of stored market data to dashboard clients (Deliverable 2E).

The stream worker is the only process connected to the provider. It writes to
PostgreSQL; this hub, one per API process, reads what was *stored* and fans it out to
connected browsers (Server-Sent Events, ``/api/v1/market-data/live``). Opening a page
therefore never opens a provider connection, and PostgreSQL stays the source of truth:
a client that misses events reloads from the REST endpoints.

One polling task runs while at least one client is connected. Each poll reads rows
stored after a primary-key cursor:

* **bars**: every new bar row (a new revision is a new row), all timeframes;
* **trades/quotes**: per asset only the last stored row and how many arrived, so a
  busy quote feed costs one small message per asset per poll.

Ids come from sequences, so a transaction that commits late can hold an id below the
cursor. Bars are therefore re-read over a trailing window of ids and de-duplicated
(``BAR_ID_OVERLAP``); a late trade or quote only matters if it is the newest, and the
next one supersedes it. A client whose queue overflows receives ``resync`` and reloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.enums import BarObservationKind, DataQualityStatus, DataSource
from app.database.models import MarketBarRecord, MarketQuoteRecord, MarketTradeRecord
from app.database.repositories import LiveUpdateRepository
from app.database.sessions import session_scope

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 1.0
MAX_BARS_PER_POLL = 2000
BAR_ID_OVERLAP = 2000
CLIENT_QUEUE_SIZE = 1000
MAX_CLIENTS = 50
# Trades and quotes are distributed when they are usable as prices (late-delivered
# STALE observations are real but not current).
DISTRIBUTED_STATUSES = (DataQualityStatus.VALID,)


class LiveBar(BaseModel):
    id: int
    symbol: str
    timeframe: str
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    vwap: Decimal | None
    trade_count: int | None
    is_complete: bool
    quality_status: DataQualityStatus
    revision: int
    source: DataSource
    observation_kind: BarObservationKind
    ingested_at: datetime


class LiveTrade(BaseModel):
    id: int
    symbol: str
    price: Decimal
    size: Decimal | None
    event_time: datetime
    received_at: datetime
    source: DataSource
    quality_status: DataQualityStatus
    arrived: int  # trades stored for this symbol since the previous message


class LiveQuote(BaseModel):
    id: int
    symbol: str
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    event_time: datetime
    received_at: datetime
    source: DataSource
    quality_status: DataQualityStatus
    arrived: int


@dataclass(frozen=True)
class LiveEvent:
    type: str  # bar | trade | quote | resync | error
    data: dict[str, Any]

    def encode(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"


@dataclass(eq=False)
class Subscription:
    symbols: dict[uuid.UUID, str]  # asset id -> Atlas symbol
    queue: asyncio.Queue[LiveEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )
    dropped: int = 0

    def offer(self, event: LiveEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # The client cannot keep up: discard the backlog and tell it to reload.
            self.dropped += self.queue.qsize()
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(LiveEvent("resync", {"reason": "client queue overflow"}))


class TooManyClientsError(Exception):
    pass


@dataclass
class _Cursor:
    bars: int
    quotes: int
    trades: int
    sent_bars: deque[int] = field(default_factory=lambda: deque(maxlen=BAR_ID_OVERLAP * 2))


class LiveUpdateHub:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        feed: str,
        clock: Clock,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self._factory = session_factory
        self._feed = feed
        self._clock = clock
        self._poll_interval = poll_interval
        self._max_clients = max_clients
        self._subscriptions: set[Subscription] = set()
        self._cursor: _Cursor | None = None
        self._task: asyncio.Task[None] | None = None
        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def clients(self) -> int:
        return len(self._subscriptions)

    async def subscribe(self, symbols: dict[uuid.UUID, str]) -> Subscription:
        if len(self._subscriptions) >= self._max_clients:
            raise TooManyClientsError(f"at most {self._max_clients} live clients")
        if self._cursor is None:
            # Start from what is stored now: clients load history over REST.
            self._cursor = await asyncio.to_thread(self._read_cursor)
        sub = Subscription(symbols=dict(symbols))
        self._subscriptions.add(sub)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live-update-hub")
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscriptions.discard(sub)
        if not self._subscriptions:
            # Idle: stop polling; the next client starts again from the current state.
            self._cursor = None
            if self._task is not None:
                self._task.cancel()
                self._task = None

    async def close(self) -> None:
        self._subscriptions.clear()
        self._cursor = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        failing = False
        while self._subscriptions:
            try:
                await self.poll_once()
                if failing:
                    failing = False
                    self._broadcast(LiveEvent("resync", {"reason": "database available again"}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the database may be unavailable; keep clients informed
                self.last_error = type(exc).__name__
                if not failing:
                    failing = True
                    logger.warning("live_updates.poll_failed", error_type=type(exc).__name__)
                    self._broadcast(
                        LiveEvent("error", {"message": "stored market data is unavailable"})
                    )
            await asyncio.sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """Read rows stored since the cursor and queue them for clients; returns events."""
        subscriptions = list(self._subscriptions)
        cursor = self._cursor
        if not subscriptions or cursor is None:
            return 0
        symbols: dict[uuid.UUID, str] = {}
        for sub in subscriptions:
            symbols.update(sub.symbols)
        events = await asyncio.to_thread(self._read_changes, cursor, symbols)
        self.last_poll_at = self._clock.now()
        self.last_error = None
        for asset_id, event in events:
            for sub in subscriptions:
                if asset_id in sub.symbols:
                    sub.offer(event)
        return len(events)

    def _broadcast(self, event: LiveEvent) -> None:
        for sub in list(self._subscriptions):
            sub.offer(event)

    # ------------------------------------------------------------------ database reads
    def _read_cursor(self) -> _Cursor:
        """Start at what is stored now. Ids already inside the late-commit window count
        as sent: they are history, which clients load over REST."""
        with session_scope(self._factory) as session:
            repo = LiveUpdateRepository(session)
            ids = repo.cursor()
            window = repo.bar_ids_any_asset(max(ids["bars"] - BAR_ID_OVERLAP, 0), ids["bars"])
        cursor = _Cursor(bars=ids["bars"], quotes=ids["quotes"], trades=ids["trades"])
        cursor.sent_bars.extend(window)
        return cursor

    def _read_changes(
        self, cursor: _Cursor, symbols: dict[uuid.UUID, str]
    ) -> list[tuple[uuid.UUID, LiveEvent]]:
        asset_ids = list(symbols)
        events: list[tuple[uuid.UUID, LiveEvent]] = []
        with session_scope(self._factory) as session:
            repo = LiveUpdateRepository(session)
            top = repo.cursor()
            # Bars committed late with an id below the cursor (checked by id only).
            sent = set(cursor.sent_bars)
            late = [
                bar_id
                for bar_id in repo.bar_ids(
                    max(cursor.bars - BAR_ID_OVERLAP, 0), cursor.bars, asset_ids, self._feed
                )
                if bar_id not in sent
            ]
            new = repo.new_bars(cursor.bars, top["bars"], asset_ids, self._feed, MAX_BARS_PER_POLL)
            for bar in [*repo.bars_by_ids(late), *new]:
                cursor.sent_bars.append(bar.id)
                events.append((bar.asset_id, _bar_event(bar, symbols[bar.asset_id])))
            # A full page means more remain: continue from it next poll.
            cursor.bars = new[-1].id if len(new) == MAX_BARS_PER_POLL else top["bars"]
            for table in ("trades", "quotes"):
                after = getattr(cursor, table)
                latest = repo.latest_since(
                    table, after, top[table], asset_ids, self._feed, DISTRIBUTED_STATUSES
                )
                for row, count in latest:
                    events.append((row.asset_id, _observation_event(row, symbols, count)))
                setattr(cursor, table, max(after, top[table]))
        return events


def _bar_event(bar: MarketBarRecord, symbol: str) -> LiveEvent:
    payload = LiveBar.model_validate({**_columns(bar), "symbol": symbol})
    return LiveEvent("bar", payload.model_dump(mode="json"))


def _observation_event(
    row: MarketQuoteRecord | MarketTradeRecord, symbols: dict[uuid.UUID, str], arrived: int
) -> LiveEvent:
    data = {**_columns(row), "symbol": symbols[row.asset_id], "arrived": arrived}
    if isinstance(row, MarketTradeRecord):
        return LiveEvent("trade", LiveTrade.model_validate(data).model_dump(mode="json"))
    return LiveEvent("quote", LiveQuote.model_validate(data).model_dump(mode="json"))


def _columns(row: Any) -> dict[str, Any]:
    return {attr.key: getattr(row, attr.key) for attr in sa_inspect(row).mapper.column_attrs}


__all__ = [
    "LiveBar",
    "LiveEvent",
    "LiveQuote",
    "LiveTrade",
    "LiveUpdateHub",
    "Subscription",
    "TooManyClientsError",
]

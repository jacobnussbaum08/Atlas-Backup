"""Shared setup for the Phase 7B simulated execution integration tests.

TEST FIXTURE material only. On top of ``execution_support`` (CONTROLLED_TEST accounts,
invented instruments, OPERATOR-sourced reservations) this writes **deterministic MOCK market
observations** (``provider_symbol = 'FIXTURE'``) into ``market_quotes``, ``market_trades`` and
``market_bars``: invented quotes, trades and one-minute bars with explicit event and
availability times. They are not market data. Every order is a simulated paper order and
every fill a simulated fill of the isolated paper broker; nothing reaches a brokerage.

Times: the risk fixtures are set at ``NOW`` (Tuesday 2026-11-03 22:30 UTC, after the US
close); ``SESSION`` is the next regular session's open (Wednesday 09:30 New York = 14:30
UTC), when equities can fill.
"""

from __future__ import annotations

import hashlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select

from app.data_engine.enums import BarObservationKind, DataQualityStatus, DataSource, PriceAdjustment
from app.database.models import MarketBarRecord, MarketQuoteRecord, MarketTradeRecord
from app.database.models.execution import (
    ExecutionAttemptRecord,
    ExecutionFillDetailRecord,
    PaperBrokerOrderRecord,
    PaperPositionRecord,
    PositionEventRecord,
)
from app.database.sessions import session_scope
from app.execution.monitor import PositionMonitor
from app.execution.simulation import ExecutionSimulator
from app.paper_broker.execution_model import DEFAULT_MODEL, ExecutionModel
from app.tests.integration.accounting_support import NOW
from app.tests.integration.execution_support import Execution, make_execution

D = Decimal
SESSION = datetime(2026, 11, 4, 14, 30, tzinfo=NOW.tzinfo)
_seq = itertools.count(1)


def _sha(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in (*parts, next(_seq))).encode()).hexdigest()


@dataclass
class Sim:
    ex: Execution
    simulator: ExecutionSimulator
    monitor: PositionMonitor

    @property
    def factory(self) -> Any:
        return self.ex.factory

    @property
    def clock(self) -> Any:
        return self.ex.risk.clock

    def at(self, when: datetime) -> None:
        self.clock.set(when)

    def later(self, seconds: float) -> datetime:
        self.clock.advance(timedelta(seconds=seconds))
        now: datetime = self.clock.now()
        return now

    # ---------------------------------------------------------------- MOCK observations
    def quote(
        self,
        asset: uuid.UUID,
        bid: str,
        ask: str,
        *,
        at: datetime | None = None,
        bid_size: str | None = "100",
        ask_size: str | None = "100",
        quality: DataQualityStatus = DataQualityStatus.VALID,
        delay: float = 0.0,
    ) -> int:
        when = at or self.clock.now()
        known = when + timedelta(seconds=delay)
        with session_scope(self.factory) as session:
            row = MarketQuoteRecord(
                asset_id=asset,
                provider="alpaca",
                feed="iex",
                provider_symbol="FIXTURE",
                event_time=when,
                event_time_ns=int(when.timestamp() * 1_000_000_000),
                conditions=[],
                tape=None,
                source=DataSource.LIVE_STREAM,
                quality_status=quality,
                content_sha256=_sha("q", asset, when, bid, ask),
                received_at=known,
                available_at=known,
                processed_at=known,
                bid_price=D(bid),
                ask_price=D(ask),
                bid_size=None if bid_size is None else D(bid_size),
                ask_size=None if ask_size is None else D(ask_size),
                bid_exchange="V",
                ask_exchange="V",
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def trade(
        self,
        asset: uuid.UUID,
        price: str,
        size: str | None = "100",
        *,
        at: datetime | None = None,
    ) -> int:
        when = at or self.clock.now()
        with session_scope(self.factory) as session:
            row = MarketTradeRecord(
                asset_id=asset,
                provider="alpaca",
                feed="iex",
                provider_symbol="FIXTURE",
                event_time=when,
                event_time_ns=int(when.timestamp() * 1_000_000_000),
                conditions=[],
                tape=None,
                source=DataSource.LIVE_STREAM,
                quality_status=DataQualityStatus.VALID,
                content_sha256=_sha("t", asset, when, price),
                received_at=when,
                available_at=when,
                processed_at=when,
                price=D(price),
                size=None if size is None else D(size),
                provider_trade_id=None,
                exchange="V",
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def bar(
        self,
        asset: uuid.UUID,
        start: datetime,
        o: str,
        h: str,
        low: str,
        c: str,
        volume: str | None = "1000",
        *,
        delay: float = 2.0,
    ) -> None:
        known = start + timedelta(minutes=1, seconds=delay)
        with session_scope(self.factory) as session:
            session.execute(
                insert(MarketBarRecord),
                [
                    {
                        "asset_id": asset,
                        "provider": "alpaca",
                        "feed": "iex",
                        "provider_symbol": "FIXTURE",
                        "timeframe": "1Min",
                        "adjustment": PriceAdjustment.RAW,
                        "event_time": start,
                        "revision": 0,
                        "open": D(o),
                        "high": D(h),
                        "low": D(low),
                        "close": D(c),
                        "volume": None if volume is None else D(volume),
                        "vwap": None,
                        "trade_count": 10,
                        "is_complete": True,
                        "source": DataSource.LIVE_STREAM,
                        "observation_kind": BarObservationKind.LIVE_INITIAL,
                        "quality_status": DataQualityStatus.VALID,
                        "received_at": known,
                        "available_at": known,
                        "original_available_at": known,
                        "processed_at": known,
                    }
                ],
            )

    # ---------------------------------------------------------------- runs
    def simulate(self) -> Any:
        return self.simulator.run_once()

    def monitor_once(self) -> Any:
        return self.monitor.run_once()

    def cycle(self, rounds: int = 3) -> None:
        """Deliver the outbox, simulate and monitor, a few times (one 'tick' of the workers)."""
        for _ in range(rounds):
            self.ex.dispatch()
            self.simulate()
            self.monitor_once()
            self.ex.dispatch()

    # ---------------------------------------------------------------- reads
    def book(self, order_id: uuid.UUID) -> PaperBrokerOrderRecord:
        with session_scope(self.factory) as session:
            row = session.scalar(
                select(PaperBrokerOrderRecord).where(
                    PaperBrokerOrderRecord.client_order_id == order_id
                )
            )
            assert row is not None
            session.expunge(row)
            return row

    def attempts(self, order_id: uuid.UUID) -> list[ExecutionAttemptRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(ExecutionAttemptRecord)
                .where(ExecutionAttemptRecord.order_id == order_id)
                .order_by(ExecutionAttemptRecord.id)
            ).all()
            session.expunge_all()
            return list(rows)

    def details(self, order_id: uuid.UUID) -> list[ExecutionFillDetailRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(ExecutionFillDetailRecord)
                .where(ExecutionFillDetailRecord.order_id == order_id)
                .order_by(ExecutionFillDetailRecord.filled_at)
            ).all()
            session.expunge_all()
            return list(rows)

    def position(self, entry_order_id: uuid.UUID) -> PaperPositionRecord | None:
        with session_scope(self.factory) as session:
            row = session.scalar(
                select(PaperPositionRecord).where(
                    PaperPositionRecord.entry_order_id == entry_order_id
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def position_events(self, position_id: uuid.UUID) -> list[str]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(PositionEventRecord.event_type)
                .where(PositionEventRecord.position_id == position_id)
                .order_by(PositionEventRecord.sequence)
            ).all()
            return [r.value for r in rows]


def make_simulation(engine: Any, *, model: ExecutionModel = DEFAULT_MODEL, **config: Any) -> Sim:
    ex = make_execution(engine, **config)
    simulator = ExecutionSimulator(ex.service, ex.risk.clock, model)
    monitor = PositionMonitor(
        ex.service, ex.risk.clock, model, accounting=ex.rv.service._accounting
    )
    return Sim(ex, simulator, monitor)


__all__ = ["NOW", "SESSION", "D", "Sim", "make_simulation"]

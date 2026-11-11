"""Integration fixtures for the Phase 8A backtester (PostgreSQL).

Every price series here is a **DETERMINISTIC TEST FIXTURE** (labelled ``FIXTURE`` in the
provider symbol): synthetic daily bars written for one test, not market data. Strategies
are the 4B fixture strategies (``strategy_support``), which are not trading strategies.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.enums import InstrumentSpecSource
from app.accounting.reference import register_instrument_spec
from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.asset_discovery.sessions import SessionDay
from app.backtesting.config import BacktestRequest
from app.backtesting.data import sessions_between
from app.backtesting.service import BacktestService
from app.core.clocks import Clock
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.database.models import MarketBarRecord, UniverseMembershipEventRecord
from app.database.sessions import session_scope
from app.strategies import catalog
from app.strategies.base import Strategy
from app.strategies.registry import StrategyRegistry
from app.tests.integration.strategy_support import ProposingStrategy, make_asset, midnight

D = Decimal
SETTLE = timedelta(hours=1)
# The run window of most tests: 20 sessions in October 2026; history starts in May.
HISTORY_FIRST = date(2026, 5, 1)
RUN_START = date(2026, 10, 1)
RUN_END = date(2026, 10, 28)


class WallClock:
    """The real time (dataset knowledge times must not precede row insertion times)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = Decimal(1_000_000)


def trend(index: int, start: float = 100.0, step: float = 0.2) -> Candle:
    """MOCK candle: a gentle uptrend with a small range (fills a limit at the prior close
    only when the next low trades through it)."""
    close = D(str(round(start + step * index, 4)))
    return Candle(close - D("0.10"), close + D("0.60"), close - D("0.60"), close)


def seed_series(
    factory: sessionmaker[Session],
    asset_id: uuid.UUID,
    candles: Callable[[int, SessionDay], Candle | None],
    *,
    first: date = HISTORY_FIRST,
    last: date = RUN_END,
    adjustments: Sequence[PriceAdjustment] = (PriceAdjustment.RAW, PriceAdjustment.SPLIT),
    split_factor: Callable[[SessionDay], Decimal] = lambda _s: D(1),
    available_after_close: timedelta = timedelta(minutes=30),
    revision: int = 0,
    ingested_at: datetime | None = None,
) -> list[SessionDay]:
    """MOCK daily bars for every session in [first, last]. ``split_factor(day)`` divides
    the RAW prices to give the SPLIT series (the provider's split-adjusted history)."""
    days = sessions_between(first, last)
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        c = candles(index, day)
        if c is None:
            continue
        known = ingested_at or day.regular_close + available_after_close
        for adjustment in adjustments:
            f = split_factor(day) if adjustment is PriceAdjustment.SPLIT else D(1)
            rows.append(
                {
                    "asset_id": asset_id,
                    "provider": "alpaca",
                    "feed": "iex",
                    "provider_symbol": "FIXTURE",
                    "timeframe": "1Day",
                    "adjustment": adjustment,
                    "event_time": midnight(day),
                    "revision": revision,
                    "open": c.open / f,
                    "high": c.high / f,
                    "low": c.low / f,
                    "close": c.close / f,
                    "volume": None if c.volume is None else c.volume * f,
                    "vwap": None,
                    "trade_count": 100,
                    "is_complete": True,
                    "source": DataSource.HISTORICAL_REST,
                    "observation_kind": BarObservationKind.HISTORICAL,
                    "quality_status": DataQualityStatus.VALID,
                    "received_at": known,
                    "available_at": known,
                    "original_available_at": None,
                    "processed_at": known,
                    "ingested_at": known,
                }
            )
    with session_scope(factory) as session:
        session.execute(insert(MarketBarRecord), rows)
    return days


def make_traded_asset(factory: sessionmaker[Session], symbol: str, **reference: Any) -> uuid.UUID:
    """A FIXTURE asset with a provider reference and a whole-share lot specification."""
    asset_id = make_asset(factory, symbol, **reference)
    with session_scope(factory) as session:
        register_instrument_spec(
            session,
            asset_id=asset_id,
            quantity_increment=D(1),
            min_quantity=D(1),
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            source=InstrumentSpecSource.OPERATOR,
            provenance="TEST FIXTURE",
            created_by="test",
        )
    return asset_id


def add_membership(
    factory: sessionmaker[Session],
    asset_id: uuid.UUID,
    layer: UniverseLayer,
    changes: Sequence[tuple[MembershipAction, datetime]],
) -> None:
    """FIXTURE membership history (each event recorded when it took effect)."""
    with session_scope(factory) as session:
        previous: int | None = None
        for sequence, (action, at) in enumerate(changes, start=1):
            row = UniverseMembershipEventRecord(
                asset_id=asset_id,
                layer=layer,
                action=action,
                sequence=sequence,
                previous_event_id=previous,
                effective_at=at,
                recorded_at=at,
                reason_code="FIXTURE",
                reason="TEST FIXTURE membership",
            )
            session.add(row)
            session.flush()
            previous = row.id


@dataclass
class Bench:
    factory: sessionmaker[Session]
    registry: StrategyRegistry
    service: BacktestService

    def request(self, **overrides: Any) -> BacktestRequest:
        values: dict[str, Any] = {
            "name": "fixture run",
            "start_date": RUN_START,
            "end_date": RUN_END,
            "initial_capital": "100000",
            "universe": {"mode": "EXPLICIT", "symbols": ["FIXA"]},
            "strategies": [{"strategy_id": "fixture_proposer"}],
            "directions": ["LONG"],
            "timeframes": ["1Day"],
            "benchmark_symbol": None,
        } | overrides
        return BacktestRequest.model_validate(values)

    def run(self, request: BacktestRequest) -> uuid.UUID:
        run_id = self.service.create(request, actor="operator:test")
        self.service.execute(run_id)
        return run_id

    def session(self) -> Session:
        return self.factory()


def make_bench(
    engine: Engine,
    strategies: Sequence[Strategy] = (ProposingStrategy(),),
    *,
    clock: Clock | None = None,
) -> Bench:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    registry = StrategyRegistry(strategies)
    with session_scope(factory) as session:
        catalog.sync_catalog(session, registry)
    service = BacktestService(
        factory, clock or WallClock(), registry=registry, version=lambda: "test-code"
    )
    return Bench(factory, registry, service)


def run_record(bench: Bench, run_id: uuid.UUID) -> Any:
    from app.database.models import BacktestRunRecord

    with session_scope(bench.factory) as session:
        run = session.get(BacktestRunRecord, run_id)
        assert run is not None
        session.expunge(run)
        return run


def rows(bench: Bench, model: Any, run_id: uuid.UUID, order: Any = None) -> list[Any]:
    with session_scope(bench.factory) as session:
        query = select(model).where(model.run_id == run_id)
        if order is not None:
            query = query.order_by(order)
        found = list(session.scalars(query))
        for r in found:
            session.expunge(r)
        return found


__all__ = [
    "HISTORY_FIRST",
    "RUN_END",
    "RUN_START",
    "SETTLE",
    "Bench",
    "Candle",
    "D",
    "WallClock",
    "add_membership",
    "make_bench",
    "make_traded_asset",
    "rows",
    "run_record",
    "seed_series",
    "trend",
]

"""Shared helpers for the Phase 3A integration tests.

Directory records come from the TEST FIXTURE ``FakeAssetDirectory``; bars and quotes
inserted by ``seed_daily_bars`` / ``seed_quotes`` are MOCK observations (constructed test
data, not market data) written straight into the tables, with controllable ingestion and
availability times for point-in-time tests.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.enums import DiscoveryJobKind, JobTrigger
from app.asset_discovery.service import AssetDiscoveryService
from app.asset_discovery.sessions import calendar_for, completed_sessions
from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.database.models import Asset, DiscoveryJobRecord, MarketBarRecord, MarketQuoteRecord
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca_assets import FakeAssetDirectory, directory_provider

# Tuesday 3 November 2026, 22:00 UTC: after the close, sessions up to 2 Nov... settled.
T0 = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
CAL = calendar_for("XNAS")

TABLES = (
    "assets",
    "provider_assets",
    "asset_reference_versions",
    "asset_identifiers",
    "asset_lifecycle_events",
    "universe_membership_events",
)


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: FixedClock
    directory: FakeAssetDirectory
    service: AssetDiscoveryService

    def discover(self, mode: str = "FULL", **params: Any) -> DiscoveryJobRecord:
        return self.run(DiscoveryJobKind.DIRECTORY_DISCOVERY, mode=mode, pipeline=False, **params)

    def run(self, kind: DiscoveryJobKind, **params: Any) -> DiscoveryJobRecord:
        created = self.service.request(
            kind, trigger=JobTrigger.CLI, requested_by="test", params=params
        )
        asyncio.run(self.service.run_due(job_id=created.job.id))
        return self.job(created.job.id)

    def job(self, job_id: uuid.UUID) -> DiscoveryJobRecord:
        with session_scope(self.factory) as session:
            job = session.get(DiscoveryJobRecord, job_id)
            assert job is not None
            session.expunge(job)
            return job

    def latest(self, kind: DiscoveryJobKind) -> DiscoveryJobRecord:
        with session_scope(self.factory) as session:
            job = session.scalars(
                select(DiscoveryJobRecord)
                .where(DiscoveryJobRecord.kind == kind)
                .order_by(DiscoveryJobRecord.created_at.desc(), DiscoveryJobRecord.id)
                .limit(1)
            ).one()
            session.expunge(job)
            return job

    def asset_id(self, symbol: str, *, active_only: bool = True) -> uuid.UUID:
        with session_scope(self.factory) as session:
            stmt = select(Asset.id).where(Asset.symbol == symbol)
            if active_only:
                stmt = stmt.where(Asset.status == "ACTIVE")
            return session.scalars(stmt).one()

    def counts(self) -> dict[str, int]:
        from sqlalchemy import table

        with session_scope(self.factory) as session:
            return {
                name: int(session.scalar(select(func.count()).select_from(table(name))) or 0)
                for name in TABLES
            }


def load_asset(session: Session, asset_id: uuid.UUID) -> Asset:
    asset = session.get(Asset, asset_id)
    assert asset is not None, asset_id
    return asset


def make_env(engine: Engine, *, now: datetime = T0, **settings: Any) -> Env:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = FixedClock(now)
    directory = FakeAssetDirectory()
    provider = directory_provider(directory, clock=clock)
    values = {"chunk_size": 10, "history_prep_max_assets": 0} | settings
    service = AssetDiscoveryService(
        factory,
        provider,
        MarketDataSettings(_env_file=None),
        AssetDiscoverySettings(_env_file=None, **values),
        clock,
    )
    return Env(factory, clock, directory, service)


def sessions_before(as_of: datetime, count: int = 70) -> list[Any]:
    return completed_sessions(CAL, as_of, count)


def seed_daily_bars(
    env: Env,
    symbol: str,
    *,
    as_of: datetime = T0,
    count: int = 70,
    close: str = "25",
    volume: int | None = 200_000,
    vwap: str | None = None,
    ingested_at: datetime | None = None,
    skip_last: int = 0,
    quality: DataQualityStatus = DataQualityStatus.VALID,
    revision: int = 0,
) -> int:
    """Insert MOCK daily bars for the ``count`` sessions completed before ``as_of``."""
    asset_id = env.asset_id(symbol)
    days = sessions_before(as_of, count)
    if skip_last:
        days = days[:-skip_last]
    known = ingested_at or (as_of - timedelta(hours=1))
    rows = []
    for index, day in enumerate(days):
        at = datetime.combine(day.day, time(0), tzinfo=CAL.zone).astimezone(UTC)
        price = Decimal(close) * (Decimal("1.01") if index % 2 else Decimal(1))
        rows.append(
            {
                "asset_id": asset_id,
                "provider": "alpaca",
                "feed": "iex",
                "provider_symbol": symbol,
                "timeframe": "1Day",
                "adjustment": PriceAdjustment.RAW,
                "event_time": at,
                "revision": revision,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": None if volume is None else Decimal(volume),
                "vwap": None if vwap is None else Decimal(vwap),
                "trade_count": 100,
                "is_complete": True,
                "source": DataSource.HISTORICAL_REST,
                "observation_kind": BarObservationKind.HISTORICAL,
                "quality_status": quality,
                "received_at": known,
                "available_at": known,
                "original_available_at": None,
                "processed_at": known,
                "ingested_at": known,
            }
        )
    with session_scope(env.factory) as session:
        session.execute(insert(MarketBarRecord), rows)
    return len(rows)


def seed_quotes(env: Env, symbol: str, *, as_of: datetime, count: int, bid: str, ask: str) -> None:
    """Insert MOCK two-sided quotes during the last completed regular session."""
    asset_id = env.asset_id(symbol)
    day = sessions_before(as_of, 1)[-1]
    rows = []
    for i in range(count):
        at = day.regular_open + timedelta(seconds=10 * i + 1)
        rows.append(
            {
                "asset_id": asset_id,
                "provider": "alpaca",
                "feed": "iex",
                "provider_symbol": symbol,
                "event_time": at,
                "event_time_ns": int(at.timestamp()) * 1_000_000_000,
                "conditions": [],
                "tape": "C",
                "source": DataSource.LIVE_STREAM,
                "quality_status": DataQualityStatus.VALID,
                "content_sha256": f"{i:064x}",
                "received_at": at,
                "available_at": at,
                "original_available_at": at,
                "processed_at": at,
                "bid_price": Decimal(bid),
                "ask_price": Decimal(ask),
                "bid_size": Decimal(1),
                "ask_size": Decimal(1),
            }
        )
    with session_scope(env.factory) as session:
        session.execute(insert(MarketQuoteRecord), rows)


__all__ = ["CAL", "T0", "Env", "make_env", "seed_daily_bars", "seed_quotes", "sessions_before"]

"""Shared setup for the news integration tests (Phase 3C).

Assets and symbol mappings here are TEST FIXTURE instruments (invented tickers), and every
article is constructed test data (``tests/fixtures/alpaca_news.py``). The scenarios cover
each mapping outcome:

* ``FXC``  - active asset, mapping assumed to cover provider history (MAPPED);
* ``OLD``  - inactive asset whose mapping ended in June 2026 (MAPPED at earlier dates);
* ``NEW``  - mapping only observed from October 2026 (NOT_EFFECTIVE before);
* ``RUZ``  - ticker that passed to another asset on 2026-03-02 12:00 (AMBIGUOUS nearby);
* anything else - UNKNOWN_SYMBOL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.enums import ValidFromBasis
from app.core.clocks import FixedClock
from app.database.models import (
    Asset,
    AssetClass,
    AssetIdentifier,
    AssetStatus,
    MappingStatus,
    NewsStreamSessionRecord,
)
from app.database.sessions import session_scope
from app.news.ingestion import NewsIngestionService
from app.news.runtime import NewsContext
from app.tests.fixtures.alpaca import FakeAlpaca
from app.tests.fixtures.alpaca_news import fake_news_provider, news_settings

HISTORY_START = datetime(2016, 1, 1, tzinfo=UTC)
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)  # a publication time used by the scenarios
NOW = datetime(2026, 3, 4, 18, 0, tzinfo=UTC)  # "now" for retrieval
RUZ_SWITCH = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@dataclass
class NewsEnv:
    engine: Engine
    factory: sessionmaker[Session]
    clock: FixedClock
    fake: FakeAlpaca
    service: NewsIngestionService
    assets: dict[str, uuid.UUID]

    def context(self, **settings: Any) -> NewsContext:
        provider = self.service.provider
        return NewsContext(
            news_settings(**settings), provider, self.service, self.factory, self.clock
        )


def _asset(
    session: Session, symbol: str, status: AssetStatus = AssetStatus.ACTIVE, name: str | None = None
) -> Asset:
    asset = Asset(
        id=uuid.uuid4(),
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        name=name or f"{symbol} Fixture Corp Common Stock",
        quote_currency="USD",
        status=status,
    )
    session.add(asset)
    session.flush()
    return asset


def _identifier(
    session: Session,
    asset: Asset,
    symbol: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
    basis: ValidFromBasis = ValidFromBasis.PROVIDER_HISTORY_ASSUMED,
) -> None:
    session.add(
        AssetIdentifier(
            asset_id=asset.id,
            provider="alpaca",
            provider_symbol=symbol,
            status=MappingStatus.RETIRED if valid_to else MappingStatus.ACTIVE,
            valid_from=valid_from,
            valid_to=valid_to,
            valid_from_basis=basis,
        )
    )
    session.flush()


def seed_assets(factory: sessionmaker[Session]) -> dict[str, uuid.UUID]:
    with session_scope(factory) as session:
        fxc = _asset(session, "FXC", name="Fixture Corp Common Stock")
        _identifier(session, fxc, "FXC", HISTORY_START)
        old = _asset(session, "OLD", AssetStatus.INACTIVE)
        _identifier(session, old, "OLD", HISTORY_START, datetime(2026, 6, 1, tzinfo=UTC))
        new = _asset(session, "NEW")
        _identifier(
            session, new, "NEW", datetime(2026, 10, 1, tzinfo=UTC), basis=ValidFromBasis.OBSERVED
        )
        ruz_a = _asset(session, "RUZ", AssetStatus.INACTIVE)
        _identifier(session, ruz_a, "RUZ", HISTORY_START, RUZ_SWITCH)
        ruz_b = _asset(session, "RUZB")
        _identifier(session, ruz_b, "RUZ", RUZ_SWITCH, basis=ValidFromBasis.OBSERVED)
        two = _asset(session, "TWO", name="Second Fixture Holdings Inc.")
        _identifier(session, two, "TWO", HISTORY_START)
        return {
            "FXC": fxc.id,
            "OLD": old.id,
            "NEW": new.id,
            "RUZ_A": ruz_a.id,
            "RUZ_B": ruz_b.id,
            "TWO": two.id,
        }


def make_news_env(engine: Engine, *, now: datetime = NOW, **settings: Any) -> NewsEnv:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = FixedClock(now)
    fake = FakeAlpaca()
    provider = fake_news_provider(fake, now=now, clock=clock, **settings)
    service = NewsIngestionService(factory, provider, news_settings(**settings), clock)
    return NewsEnv(engine, factory, clock, fake, service, seed_assets(factory))


def stream_session(factory: sessionmaker[Session], at: datetime) -> uuid.UUID:
    with session_scope(factory) as session:
        record = NewsStreamSessionRecord(
            id=uuid.uuid4(),
            provider="alpaca",
            worker_id="test-worker",
            state="SUBSCRIBED",
            url="ws://test",
            started_at=at,
        )
        session.add(record)
        session.flush()
        return record.id


def later(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


__all__ = [
    "NOW",
    "RUZ_SWITCH",
    "T0",
    "NewsEnv",
    "later",
    "make_news_env",
    "stream_session",
]

"""REAL Alpaca asset directory test (opt-in). Uses the credentials in the local ``.env``.

Reads ``GET /v2/assets`` from the paper Trading API host (decision D-050) and runs the
whole discovery + qualification pipeline into a throw-away database, with a small, bounded
history preparation through the market-data API. Every HTTP request is recorded and
checked: only GETs to the asset paths of the paper host and to the market-data host.
Prints counts only.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    JobTrigger,
    QualificationOutcome,
    UniverseLayer,
)
from app.asset_discovery.membership import current_members
from app.asset_discovery.service import AssetDiscoveryService
from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.providers.alpaca.reference import ALLOWED_PATH, PAPER_TRADING_API_HOST
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.database.models import (
    Asset,
    DiscoveryJobRecord,
    ProviderAssetRecord,
    QualificationEvaluationRecord,
)
from app.database.sessions import session_scope

pytestmark = [pytest.mark.live, pytest.mark.integration]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.requests: list[tuple[str, str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.host, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def test_real_directory_discovery_and_qualification(fresh_engine):
    settings = get_alpaca_settings()
    if not settings.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    transport = RecordingTransport()
    clock = SystemClock()
    provider = AlpacaMarketDataProvider(settings, clock, max_retries=3, transport=transport)
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    market = MarketDataSettings(_env_file=None)
    service = AssetDiscoveryService(
        factory,
        provider,
        market,
        AssetDiscoverySettings(_env_file=None, history_prep_max_assets=12),
        clock,
        ingestion=HistoricalIngestionService(factory, provider, market, clock),
    )

    created = service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.CLI,
        requested_by="live-test",
        params={"mode": "FULL", "pipeline": True},
    )
    asyncio.run(service.run_due(job_id=created.job.id, max_jobs=10))

    with session_scope(factory) as session:
        jobs = {
            j.kind.value
            + (
                "" if j.kind is not DiscoveryJobKind.MEMBERSHIP_UPDATE else ":" + j.params["layer"]
            ): j
            for j in session.scalars(
                select(DiscoveryJobRecord).order_by(DiscoveryJobRecord.created_at)
            )
        }
        discovery = jobs["DIRECTORY_DISCOVERY"]
        assert discovery.status in (
            DiscoveryJobStatus.COMPLETED,
            DiscoveryJobStatus.PARTIALLY_COMPLETED,
        )
        catalogued = session.scalar(select(func.count()).select_from(ProviderAssetRecord)) or 0
        active = session.scalar(
            select(func.count()).select_from(Asset).where(Asset.status == "ACTIVE")
        )
        assert catalogued > 10_000  # Alpaca lists tens of thousands of us_equity records
        # Operator-registered assets were adopted, not duplicated.
        assert (
            session.scalar(
                select(func.count())
                .select_from(Asset)
                .where(Asset.symbol == "AAPL", Asset.status == "ACTIVE")
            )
            == 1
        )
        qualification = jobs["QUALIFICATION"]
        assert qualification.status in (
            DiscoveryJobStatus.COMPLETED,
            DiscoveryJobStatus.PARTIALLY_COMPLETED,
        )
        outcomes = dict(
            session.execute(
                select(QualificationEvaluationRecord.outcome, func.count())
                .where(QualificationEvaluationRecord.job_id == qualification.id)
                .group_by(QualificationEvaluationRecord.outcome)
            ).all()
        )
        qualified = current_members(session, UniverseLayer.QUALIFIED_UNIVERSE)
        watchlist = current_members(session, UniverseLayer.ACTIVE_WATCHLIST)
        assert outcomes.get(QualificationOutcome.QUALIFIED, 0) == len(qualified)
        assert set(watchlist) <= set(qualified)
        print(
            json.dumps(
                {
                    "catalogued": catalogued,
                    "active": active,
                    "discovery_counters": discovery.counters,
                    "outcomes": {k.value: v for k, v in outcomes.items()},
                    "qualified": len(qualified),
                    "watchlist": len(watchlist),
                    "history_preparation": qualification.summary.get("history_preparation"),
                    "jobs": {k: v.status.value for k, v in jobs.items()},
                },
                indent=1,
                default=str,
            )
        )

    # The fence held: nothing but asset GETs on the paper host, market data elsewhere.
    for method, host, path in transport.requests:
        assert method == "GET"
        if host == PAPER_TRADING_API_HOST:
            assert ALLOWED_PATH.match(path), path
        else:
            assert host == "data.alpaca.markets", host
    assert any(host == PAPER_TRADING_API_HOST for _, host, _ in transport.requests)

    # Single lookups by provider ID work (metadata refresh).
    refresh = service.request(
        DiscoveryJobKind.METADATA_REFRESH,
        trigger=JobTrigger.CLI,
        requested_by="live-test",
        params={"max_assets": 3},
    )
    asyncio.run(service.run_due(job_id=refresh.job.id, max_jobs=1))
    with session_scope(factory) as session:
        done = session.get(DiscoveryJobRecord, refresh.job.id)
        assert done is not None
        assert done.status is DiscoveryJobStatus.COMPLETED
        assert done.counters["verified"] == 3
    asyncio.run(transport.aclose())

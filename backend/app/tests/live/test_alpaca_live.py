"""REAL Alpaca smoke test (opt-in). Uses the credentials in the local ``.env``.

Verifies against the live market-data API: authentication, retrieval for AAPL and
SPY on the configured feed, normalization, quality checks, persistence, read-back
and idempotent repetition. Never calls trading endpoints. Prints counts only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import DataQualityStatus, IngestionJobStatus, PriceAdjustment
from app.data_engine.historical import HistoricalFetchRequest, HistoricalIngestionService
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.schemas import CredentialStatus
from app.database.models import DataQualityEventRecord, MarketBarRecord
from app.database.repositories import MarketBarRepository, SeriesKey
from app.database.sessions import session_scope

pytestmark = [pytest.mark.live, pytest.mark.integration]

SYMBOLS = ("AAPL", "SPY")
INTRADAY = ("1Min", "5Min", "15Min", "1Hour")


@pytest.fixture
def provider() -> AlpacaMarketDataProvider:
    settings = get_alpaca_settings()  # reads the real local .env
    if not settings.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    return AlpacaMarketDataProvider(settings, SystemClock(), max_retries=3, max_pages=200)


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, list(SYMBOLS), "alpaca", load_reference_assets())
    return factory


def _service(factory, provider, batch: int) -> HistoricalIngestionService:
    settings = MarketDataSettings(_env_file=None, historical_data_batch_size=batch)
    return HistoricalIngestionService(factory, provider, settings, SystemClock())


def _run(service, **request):
    job = service.create_job(HistoricalFetchRequest(symbols=SYMBOLS, **request), "live-test")
    return asyncio.run(service.run_job(job.job_id))


def _report(label, job, factory):
    with session_scope(factory) as session:
        statuses = dict(
            session.execute(
                select(MarketBarRecord.quality_status, func.count())
                .where(MarketBarRecord.ingestion_job_id == job.job_id)
                .group_by(MarketBarRecord.quality_status)
            ).all()
        )
        events = dict(
            session.execute(
                select(DataQualityEventRecord.event_type, func.count())
                .where(DataQualityEventRecord.ingestion_job_id == job.job_id)
                .group_by(DataQualityEventRecord.event_type)
            ).all()
        )
    c = job.counters
    by_status = {k.value: v for k, v in statuses.items()}
    by_event = {k.value: v for k, v in events.items()}
    per_symbol = [
        (s.provider_symbol, s.status.value, s.counters.records_stored) for s in job.symbols
    ]
    # Smoke-test evidence: counts only, never credentials.
    print(
        f"[live] {label}: status={job.status.value} pages={c.pages_retrieved} "
        f"retrieved={c.records_retrieved} stored={c.records_stored} "
        f"duplicate={c.records_duplicate} rejected={c.records_rejected} "
        f"quarantined={c.records_quarantined} by_status={by_status} "
        f"events={by_event} symbols={per_symbol}"
    )


def test_live_alpaca_historical_bars(provider, factory):
    auth = asyncio.run(provider.check_authentication())
    print(f"[live] authentication: {auth.status.value} feed={provider.feed}")
    assert auth.status is CredentialStatus.VERIFIED

    now = datetime.now(UTC)
    daily = _run(
        _service(factory, provider, batch=1000),
        timeframe="1Day",
        start=now - timedelta(days=21),
        end=now - timedelta(days=1),
    )
    _report("1Day", daily, factory)
    assert daily.status is IngestionJobStatus.COMPLETED
    assert all(s.counters.records_stored > 0 for s in daily.symbols), "both symbols returned bars"

    # Pick the most recent fully completed trading day that Alpaca returned.
    with session_scope(factory) as session:
        last_day = session.scalar(
            select(func.max(MarketBarRecord.event_time)).where(
                MarketBarRecord.ingestion_job_id == daily.job_id,
                MarketBarRecord.quality_status == DataQualityStatus.VALID,
            )
        )
    assert last_day is not None
    session_start = last_day + timedelta(hours=4)  # 04:00 New York: pre-market start
    session_end = last_day + timedelta(hours=20)  # 20:00 New York: after-hours end

    for timeframe in INTRADAY:
        # A small page size forces real pagination for minute bars.
        job = _run(
            _service(factory, provider, batch=100),
            timeframe=timeframe,
            start=session_start,
            end=session_end,
        )
        _report(timeframe, job, factory)
        assert job.status is IngestionJobStatus.COMPLETED
        assert job.counters.records_stored > 0
        if timeframe == "1Min":
            assert job.counters.pages_retrieved > len(SYMBOLS), "pagination exercised"

    # Read back from PostgreSQL through the repository.
    with session_scope(factory) as session:
        aapl = daily.symbols[0].asset_id
        key = SeriesKey(aapl, "1Day", "alpaca", provider.feed, PriceAdjustment.RAW)
        bars = MarketBarRepository(session).current_bars(
            key, start=now - timedelta(days=30), end=now, statuses=[DataQualityStatus.VALID]
        )
    assert bars
    assert all(b.feed == provider.feed and b.original_available_at is None for b in bars)
    print(f"[live] read back {len(bars)} AAPL daily bars from PostgreSQL")

    # Repeating the same request must not create duplicates.
    repeat = _run(
        _service(factory, provider, batch=1000),
        timeframe="1Day",
        start=now - timedelta(days=21),
        end=now - timedelta(days=1),
    )
    _report("1Day repeat", repeat, factory)
    assert repeat.counters.records_stored == 0
    assert repeat.counters.records_duplicate == daily.counters.records_stored

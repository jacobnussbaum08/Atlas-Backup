"""REAL Alpaca recovery tests (opt-in, Deliverable 2D). Credentials from the local ``.env``.

Run from the repository root (never in ordinary test runs):

    ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... uv run pytest -m live -s \\
        backend/app/tests/live/test_alpaca_recovery_live.py

* ``test_real_outage_*``: records a live-stream interruption over 20 minutes of the
  previous trading day's regular session (when IEX certainly traded AAPL and SPY), then
  runs the production recovery path against Alpaca's real history: bars and trades must
  be retrieved, verified and stored as backfill; quotes (not enabled) must stay missing;
  repeating the recovery must store nothing twice.
* ``test_real_closed_market_*``: an interruption over the last weekend loses nothing and
  needs no provider call.

Market-data endpoints only (never trading). Only counts are printed, never credentials.
The database is a throw-away copy of the migrated template.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.calendar import local_date, trading_day
from app.data_engine.enums import (
    DataSource,
    DataType,
    GapRecoveryStatus,
    GapStatus,
    RecoveryJobStatus,
    StreamGapKind,
)
from app.data_engine.historical import HistoricalFetchRequest
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.recovery import RecoveryService
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.timeframes import Timeframe
from app.database.models import (
    MarketBarRecord,
    MarketDataGapRecord,
    MarketTradeRecord,
    RecoveryJobRecord,
    StreamGapRecord,
)
from app.database.repositories import StreamRepository
from app.database.sessions import session_scope

pytestmark = [pytest.mark.live, pytest.mark.integration]

SYMBOLS = ("AAPL", "SPY")


@pytest.fixture
def credentials():
    settings = get_alpaca_settings()
    if not settings.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    return settings


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, list(SYMBOLS), "alpaca", load_reference_assets())
    return factory


def _service(factory, credentials) -> RecoveryService:
    settings = MarketDataSettings(
        _env_file=None,
        market_data_symbols=",".join(SYMBOLS),
        market_data_recovery_timeframes="1Min",
        market_data_recovery_data_types="bars,trades",
        market_data_completeness_lookback_days=0,
    )
    provider = AlpacaMarketDataProvider(credentials, SystemClock())
    assert provider.feed == "iex", "recovery is verified on the configured IEX feed"
    return RecoveryService(factory, provider, settings, SystemClock(), worker_id="live-test")


def _previous_regular_session(now: datetime) -> tuple[datetime, datetime]:
    day = local_date(now) - timedelta(days=1)
    while (found := trading_day(day)) is None:
        day -= timedelta(days=1)
    return found.regular_open, found.regular_close


def _stream_gap(factory, kind, start, end) -> int:
    with session_scope(factory) as session:
        return (
            StreamRepository(session)
            .open_gap(
                provider="alpaca",
                feed="iex",
                kind=kind,
                gap_start=start,
                gap_end=end,
                reason="live test: simulated interruption over a past interval",
                symbols=list(SYMBOLS),
                channels=["bars", "trades", "quotes"],
                detected_at=end,
                previous_session_id=None,
                last_message_before=start,
            )
            .id
        )


def test_real_outage_is_recovered_from_alpaca_history(factory, credentials):
    now = datetime.now(UTC)
    regular_open, _ = _previous_regular_session(now)
    outage = (regular_open + timedelta(minutes=30), regular_open + timedelta(minutes=50))
    stream_gap = _stream_gap(factory, StreamGapKind.WORKER_NOT_RUNNING, *outage)
    service = _service(factory, credentials)

    service.translate_stream_gaps(now)
    scheduled = service.schedule(now)
    outcomes = asyncio.run(service.run_due(max_jobs=20))
    service.settle_stream_gaps(datetime.now(UTC))

    with session_scope(factory) as session:
        gaps = session.scalars(
            select(MarketDataGapRecord).where(MarketDataGapRecord.stream_gap_id == stream_gap)
        ).all()
        bars = session.scalar(select(func.count()).select_from(MarketBarRecord)) or 0
        trades = session.scalar(select(func.count()).select_from(MarketTradeRecord)) or 0
        sources = set(session.scalars(select(MarketTradeRecord.source).distinct()))
        status = session.get(StreamGapRecord, stream_gap)
        jobs = session.scalars(select(RecoveryJobRecord)).all()
    summary = {
        (g.provider_symbol, g.data_type.value): (g.status.value, g.records_recovered) for g in gaps
    }
    print(
        f"\nreal outage {outage[0].isoformat()} - {outage[1].isoformat()} (IEX): "
        f"jobs scheduled={scheduled} outcomes={sorted(outcomes)} bars={bars} trades={trades}"
    )
    for key, value in sorted(summary.items()):
        print(f"  {key}: {value}")
    assert status is not None
    print(f"  stream gap recovery_status={status.recovery_status.value}")

    for symbol in SYMBOLS:
        bar_status, bar_count = summary[(symbol, DataType.BAR.value)]
        trade_status, trade_count = summary[(symbol, DataType.TRADE.value)]
        assert bar_status == GapStatus.RECOVERED.value
        assert trade_status == GapStatus.RECOVERED.value
        assert 0 < bar_count <= 21  # at most one bar per minute of the interval (+1 before)
        assert trade_count > 0
        assert summary[(symbol, DataType.QUOTE.value)][0] == GapStatus.OPEN.value
    assert {j.status for j in jobs} == {RecoveryJobStatus.COMPLETED}
    assert sources == {DataSource.HISTORICAL_REST}
    assert status.recovery_status is GapRecoveryStatus.PARTIALLY_RECOVERED  # quotes missing

    # Asking again: the interval is verified, so nothing is requested or stored twice.
    minute = Timeframe.parse("1Min")
    assert service.request_recovery("AAPL", DataType.BAR, minute, *outage, "live-test") == []
    historical = service._historical
    job = historical.create_job(
        HistoricalFetchRequest(
            symbols=("AAPL",),
            data_type=DataType.TRADE,
            start=outage[0],
            end=outage[1] - timedelta(microseconds=1),
        ),
        requested_by="live-test",
    )
    repeat = asyncio.run(historical.run_job(job.job_id))
    print(
        f"  repeated AAPL trade retrieval: retrieved={repeat.counters.records_retrieved} "
        f"stored={repeat.counters.records_stored} duplicate={repeat.counters.records_duplicate}"
    )
    assert repeat.counters.records_stored == 0
    assert repeat.counters.records_duplicate == repeat.counters.records_retrieved > 0


def test_real_closed_market_outage_is_not_missing_data(factory, credentials):
    now = datetime.now(UTC)
    saturday = local_date(now) - timedelta(days=(local_date(now).weekday() - 5) % 7 or 7)
    start = datetime(saturday.year, saturday.month, saturday.day, 16, tzinfo=UTC)
    stream_gap = _stream_gap(
        factory, StreamGapKind.WORKER_NOT_RUNNING, start, start + timedelta(hours=24)
    )
    service = _service(factory, credentials)
    assert service.translate_stream_gaps(now) == 1
    with session_scope(factory) as session:
        gaps = session.scalar(select(func.count()).select_from(MarketDataGapRecord)) or 0
        status = session.get(StreamGapRecord, stream_gap)
    print(f"\nweekend outage {start.isoformat()} +24h: series gaps={gaps}")
    assert gaps == 0
    assert status is not None
    assert status.recovery_status is GapRecoveryStatus.RECOVERED

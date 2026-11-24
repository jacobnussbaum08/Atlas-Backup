"""REAL Alpaca data through the dashboard endpoints (opt-in, Deliverable 2E).

Run from the repository root (never in ordinary test runs):

    ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... uv run pytest -m live -s \\
        backend/app/tests/live/test_dashboard_live.py

Retrieves the previous regular session's 1-minute bars, the daily bars around it and the
session's last trades from Alpaca's market-data API (never trading endpoints) into a
throw-away database, then reads them back through the dashboard endpoints:

* ``/candles`` returns what the chart draws: strictly ascending, one bar per minute, on
  the minute grid, inside the session, with valid OHLC;
* ``/snapshots`` derives the last price from the newest stored trade and the change from
  the stored daily bar of the session before, with the arithmetic checked here.

Only counts and prices are printed, never credentials.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.clocks import FixedClock, SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.calendar import local_date, previous_trading_day, trading_day
from app.data_engine.enums import DataType
from app.data_engine.historical import HistoricalFetchRequest, HistoricalIngestionService
from app.data_engine.market_overview import session_midnight
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.runtime import MarketDataContext
from app.database.sessions import session_scope
from app.main import create_app

pytestmark = [pytest.mark.live, pytest.mark.integration]

SYMBOLS = ("AAPL", "SPY")
BASE = "/api/v1/market-data"


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


def test_real_history_is_served_correctly_to_the_dashboard(provider, factory, make_settings):
    now = datetime.now(UTC)
    day = local_date(now) - timedelta(days=1)
    while (session := trading_day(day)) is None:
        day -= timedelta(days=1)
    before = previous_trading_day(session.day)
    md_settings = MarketDataSettings(_env_file=None)
    service = HistoricalIngestionService(factory, provider, md_settings, SystemClock())

    def run(**request) -> None:
        job = service.create_job(HistoricalFetchRequest(symbols=SYMBOLS, **request), "live-test")
        result = asyncio.run(service.run_job(job.job_id))
        c = result.counters
        label = f"{request.get('data_type', DataType.BAR).value} {request.get('timeframe')}"
        print(
            f"[live] {label}: status={result.status.value} "
            f"retrieved={c.records_retrieved} stored={c.records_stored}"
        )

    run(timeframe="1Day", start=session_midnight(before.day), end=session_midnight(session.day))
    run(
        timeframe="1Min",
        start=session.regular_open,
        end=session.regular_close - timedelta(minutes=1),
    )
    run(
        data_type=DataType.TRADE,
        start=session.regular_close - timedelta(minutes=5),
        end=session.regular_close - timedelta(microseconds=1),
    )

    context = MarketDataContext(
        settings=md_settings, provider=provider, service=service, session_factory=factory
    )
    # Read as of shortly after that session's close.
    as_of = session.regular_close + timedelta(minutes=30)
    app = create_app(
        make_settings(), clock=FixedClock(as_of), health_checks={}, market_data=context
    )
    with TestClient(app) as client:
        for symbol in SYMBOLS:
            body = client.get(
                f"{BASE}/candles",
                params={
                    "symbol": symbol,
                    "timeframe": "1Min",
                    "start": session.regular_open.isoformat(),
                    "end": session.regular_close.isoformat(),
                    "limit": 10_000,
                },
            ).json()
            times = [datetime.fromisoformat(c["event_time"]) for c in body["candles"]]
            assert times == sorted(set(times)), "strictly ascending, one candle per minute"
            assert all(t.second == 0 and t.microsecond == 0 for t in times)
            assert all(session.regular_open <= t < session.regular_close for t in times)
            for c in body["candles"]:
                low, high = Decimal(c["low"]), Decimal(c["high"])
                assert low <= Decimal(c["open"]) <= high
                assert low <= Decimal(c["close"]) <= high
            assert body["feed"] == "iex"
            assert len(times) > 300  # a liquid symbol trades on IEX in most minutes
            print(f"[live] {symbol} 1Min candles {session.day}: {len(times)} of 390 minutes")

        snapshots = client.get(f"{BASE}/snapshots").json()["snapshots"]
    for snap in snapshots:
        symbol = snap["asset"]["symbol"]
        last = snap["last_price"]
        previous = snap["previous_close"]
        assert last is not None
        assert last["basis"] == "TRADE"
        assert last["source"] == "HISTORICAL_REST"
        assert previous is not None, snap["previous_close_note"]
        assert previous["session_date"] == before.day.isoformat()
        change = Decimal(last["value"]) - Decimal(previous["value"])
        assert Decimal(snap["change"]) == change
        expected_pct = (change / Decimal(previous["value"]) * 100).quantize(Decimal("0.0001"))
        assert Decimal(snap["change_percent"]) == expected_pct
        assert snap["freshness"] in ("MARKET_CLOSED", "STALE", "HISTORICAL")
        print(
            f"[live] {symbol}: last trade {last['value']} at {last['event_time']}, "
            f"previous IEX daily close {previous['value']} ({previous['session_date']}), "
            f"change {snap['change']} ({snap['change_percent']}%), freshness {snap['freshness']}"
        )

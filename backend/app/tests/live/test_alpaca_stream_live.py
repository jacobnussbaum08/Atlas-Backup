"""REAL Alpaca streaming smoke tests (opt-in). Use the credentials in the local ``.env``.

Run from the repository root (never in ordinary test runs):

    ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... uv run pytest -m live -s \
        backend/app/tests/live/test_alpaca_stream_live.py

* ``test_real_iex_stream_*``: the production worker against
  ``wss://stream.data.alpaca.markets/v2/iex`` for AAPL and SPY, persisting into a
  throw-away database. Whether market messages arrive depends on the time: IEX has
  data in its trading sessions only. During the regular session the test requires
  persisted trades and quotes; outside it, it verifies authentication and subscription
  and reports that no data was expected.
* ``test_real_reconnection_*``: drops the real socket and verifies Alpaca accepts the
  re-authentication and the restored subscription.
* ``test_alpaca_test_stream_*``: Alpaca's official *test* stream (``/v2/test``, symbol
  ``FAKEPACA``), available around the clock. Its data is synthetic by Alpaca's design;
  it verifies the live protocol end to end and is never persisted as market data.

Only counts and states are printed; credentials never are. Alpaca allows one
connection per endpoint, so stop any running stream worker before these tests.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import StreamChannel, StreamSubscriptionState
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.providers.alpaca.stream import AlpacaMarketDataStream
from app.data_engine.providers.base import StreamSubscription
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.stream_health import MarketSession, us_equity_session
from app.database.models import (
    MarketBarRecord,
    MarketQuoteRecord,
    MarketTradeRecord,
    RawMarketDataEventRecord,
    StreamGapRecord,
    StreamSessionRecord,
)
from app.database.sessions import session_scope
from app.workers.market_data_stream import EXIT_OK, StreamWorker

pytestmark = [pytest.mark.live, pytest.mark.integration]

SYMBOLS = ("AAPL", "SPY")
SECONDS = float(os.environ.get("ATLAS_LIVE_STREAM_SECONDS", "45"))
ALL_CHANNELS = "bars,updated_bars,daily_bars,trades,quotes"


@pytest.fixture
def credentials():
    settings = get_alpaca_settings()  # reads the real local .env
    if not settings.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    return settings


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, list(SYMBOLS), "alpaca", load_reference_assets())
    return factory


def _worker(factory, credentials) -> StreamWorker:
    settings = MarketDataSettings(
        _env_file=None,
        market_data_stream_enabled=True,
        market_data_symbols=",".join(SYMBOLS),
        market_data_stream_channels=ALL_CHANNELS,
        market_data_reconnect_max_delay=10,
    )
    provider = AlpacaMarketDataProvider(credentials, SystemClock())
    assert provider.feed == "iex", "smoke tests verify the configured IEX feed"
    return StreamWorker(
        session_factory=factory, provider=provider, settings=settings, heartbeat_interval=1.0
    )


def _count(factory, model) -> int:
    with session_scope(factory) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


async def _until(check, timeout: float) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not check():
                await asyncio.sleep(0.2)
    except TimeoutError:
        return False
    return True


def test_real_iex_stream_authenticates_subscribes_and_persists(factory, credentials):
    session_at_start = us_equity_session(datetime.now(UTC))

    async def scenario():
        worker = _worker(factory, credentials)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        subscribed = await _until(lambda: worker.state.value == "SUBSCRIBED", 30)
        await asyncio.sleep(SECONDS)
        stop.set()
        return worker, subscribed, await asyncio.wait_for(task, 60)

    worker, subscribed, code = asyncio.run(scenario())
    counts = {
        "frames_received": worker.counters["frames_received"],
        "bars": _count(factory, MarketBarRecord),
        "quotes": _count(factory, MarketQuoteRecord),
        "trades": _count(factory, MarketTradeRecord),
        "raw_frames": _count(factory, RawMarketDataEventRecord),
        "rejected": worker.counters["records_rejected"],
        "quarantined": worker.counters["records_quarantined"],
        "dropped": worker.counters["frames_dropped"],
    }
    print(f"\nlive IEX stream ({session_at_start.value}, {SECONDS:.0f}s): {counts}")
    print(f"confirmed subscription: {worker.confirmed.as_dict()}")

    assert subscribed, f"not subscribed: {worker.last_error}"
    assert code == EXIT_OK
    assert worker.auth_state.value in ("AUTHENTICATED", "NOT_ATTEMPTED")  # reset on shutdown
    assert worker.last_authenticated_at is not None
    expected = StreamSubscription.of(frozenset(StreamChannel), frozenset(SYMBOLS))
    assert expected.missing_from(worker.confirmed) == {}
    assert counts["dropped"] == 0
    with session_scope(factory) as session:
        (record,) = session.scalars(select(StreamSessionRecord)).all()
        assert record.authenticated_at is not None
        assert record.subscribed_at is not None
        assert record.url == "wss://stream.data.alpaca.markets/v2/iex"
    if session_at_start is MarketSession.REGULAR:
        assert counts["trades"] > 0
        assert counts["quotes"] > 0
    elif counts["frames_received"] == 0:
        print(f"no IEX messages: none expected in session {session_at_start.value}")


def test_real_reconnection_restores_the_subscription(factory, credentials):
    async def scenario():
        worker = _worker(factory, credentials)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        assert await _until(lambda: worker.state.value == "SUBSCRIBED", 30), worker.last_error
        first = worker.confirmed
        # Simulated network failure: abort the TCP connection without a close frame.
        worker.current_stream._ws.transport.abort()
        restored = await _until(
            lambda: worker.reconnect_count >= 1 and worker.state.value == "SUBSCRIBED", 90
        )
        second = worker.confirmed
        stop.set()
        code = await asyncio.wait_for(task, 60)
        return worker, first, second, restored, code

    worker, first, second, restored, code = asyncio.run(scenario())
    print(
        f"\nreconnects={worker.reconnect_count} errors="
        f"{[e['message'] for e in worker.recent_errors]}"
    )
    assert restored, worker.last_error
    assert code == EXIT_OK
    assert first == second
    assert worker.subscription_state in (
        StreamSubscriptionState.NONE,
        StreamSubscriptionState.ACTIVE,
    )
    with session_scope(factory) as session:
        sessions = session.scalars(select(StreamSessionRecord)).all()
        gaps = session.scalars(select(StreamGapRecord)).all()
    assert sum(1 for s in sessions if s.subscribed_at is not None) >= 2
    assert len(gaps) == 1
    assert gaps[0].gap_end is not None


def test_alpaca_test_stream_delivers_messages_end_to_end(credentials):
    """Alpaca's official test stream: real server and protocol, synthetic FAKEPACA data."""

    async def scenario():
        stream = AlpacaMarketDataStream(
            url="wss://stream.data.alpaca.markets/v2/test",
            feed="iex",  # label only; nothing from this test is persisted
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            clock=SystemClock(),
        )
        await stream.connect()
        subscription = StreamSubscription.of(
            frozenset({StreamChannel.TRADES, StreamChannel.QUOTES, StreamChannel.BARS}),
            frozenset({"FAKEPACA"}),
        )
        confirmed = await stream.subscribe(subscription)
        kinds: Counter[str] = Counter()

        async def consume():
            async for batch in stream.events():
                kinds["trade"] += len(batch.trades)
                kinds["quote"] += len(batch.quotes)
                kinds["bar"] += len(batch.bars)
                kinds["malformed"] += len(batch.malformed)
                for trade in batch.trades:
                    assert trade.event_time_ns is not None
                if kinds["trade"] + kinds["quote"] >= 4:
                    return

        await asyncio.wait_for(consume(), 60)
        await stream.close()
        return confirmed, kinds

    confirmed, kinds = asyncio.run(scenario())
    print(f"\nAlpaca test stream (synthetic FAKEPACA): {dict(kinds)}")
    assert confirmed.trades == {"FAKEPACA"}
    assert kinds["trade"] + kinds["quote"] >= 4
    assert kinds["malformed"] == 0

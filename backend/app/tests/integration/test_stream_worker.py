"""Stream worker against real PostgreSQL and a LOCAL FAKE Alpaca WebSocket server.

MOCK-PROVIDER TESTS: every market message here is constructed test data sent by
``tests/fixtures/alpaca_stream.py``. The database, migrations, constraints, triggers,
transactions, the WebSocket client and the worker are real. Nothing here claims
connectivity to Alpaca; see ``tests/live/test_alpaca_stream_live.py`` for that.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    BarObservationKind,
    CoverageEvidence,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    DataType,
    GapRecoveryStatus,
    PriceAdjustment,
    StreamGapKind,
    StreamHealth,
)
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import AlpacaSettings
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.runtime import MarketDataContext
from app.database.models import (
    Asset,
    AuditLog,
    CoverageRecord,
    DataQualityEventRecord,
    MarketBarRecord,
    MarketQuoteRecord,
    MarketTradeRecord,
    RawMarketDataEventRecord,
    StreamGapRecord,
    StreamSessionRecord,
    StreamStatusRecord,
)
from app.database.repositories import MarketBarRepository, SeriesKey
from app.database.sessions import session_scope
from app.main import create_app
from app.tests.fixtures.alpaca_stream import (
    FAKE_STREAM_KEY,
    FAKE_STREAM_SECRET,
    FakeAlpacaStreamServer,
    Script,
    bar_msg,
    eventually,
    frame,
    quote_msg,
    trade_msg,
)
from app.workers.market_data_stream import (
    EXIT_ALREADY_RUNNING,
    EXIT_CONFIGURATION,
    EXIT_OK,
    EXIT_STREAM_FAILED,
    INTERRUPTED_REASON,
    StreamWorker,
)

pytestmark = pytest.mark.integration

MINUTE = "2026-01-02T14:30:00Z"
MINUTE_DT = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    return factory


def _settings(**values) -> MarketDataSettings:
    base: dict[str, Any] = {
        "market_data_stream_enabled": True,
        "market_data_symbols": "AAPL,SPY",
        "market_data_stream_channels": "bars,updated_bars,trades,quotes",
        "market_data_reconnect_max_delay": 0.5,
        # Fixture events are dated 2026-01-02 and replayed now: staleness (2D) has its own
        # tests and is disabled for these.
        "market_data_stale_observation_seconds": 0,
    }
    return MarketDataSettings(_env_file=None, **(base | values))


def _provider(url: str) -> AlpacaMarketDataProvider:
    alpaca = AlpacaSettings(_env_file=None, api_key=FAKE_STREAM_KEY, secret_key=FAKE_STREAM_SECRET)
    return AlpacaMarketDataProvider(alpaca, SystemClock(), stream_url=url, heartbeat_timeout=5)


def _worker(
    factory, url: str, settings: MarketDataSettings | None = None, **kwargs
) -> StreamWorker:
    options: dict[str, Any] = {
        "heartbeat_interval": 0.1,
        "backoff_base": 0.05,
        "flush_interval": 0.02,
        "drain_timeout": 3.0,
        "persist_retry_max": 0.2,
        "jitter": lambda: 1.0,
    } | kwargs
    return StreamWorker(
        session_factory=factory,
        provider=_provider(url),
        settings=settings or _settings(),
        clock=SystemClock(),
        **options,
    )


async def _run_until(worker: StreamWorker, until: Callable[[], bool], timeout: float = 10.0):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await eventually(lambda: until() or task.done(), timeout)
    finally:
        stop.set()
    code = await asyncio.wait_for(task, 20)
    leftovers = [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith("stream-") and not t.done()
    ]
    assert leftovers == []  # no orphaned worker tasks after shutdown
    return code


def _count(factory, model, *where) -> int:
    with session_scope(factory) as session:
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _rows(factory, model, *where, order=None):
    with session_scope(factory) as session:
        stmt = select(model).where(*where)
        if order is not None:
            stmt = stmt.order_by(order)
        return list(session.scalars(stmt))


def _status(factory) -> StreamStatusRecord:
    with session_scope(factory) as session:
        row = session.get(StreamStatusRecord, ("alpaca", "iex"))
        assert row is not None
        return row


def _event_types(factory) -> list[DataQualityEventType]:
    return [e.event_type for e in _rows(factory, DataQualityEventRecord)]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ initialization
def test_disabled_worker_records_disabled_and_exits(factory):
    async def scenario():
        worker = _worker(
            factory, "ws://127.0.0.1:9/v2/iex", _settings(market_data_stream_enabled=False)
        )
        return await worker.run(asyncio.Event())

    assert run(scenario()) == EXIT_OK
    status = _status(factory)
    assert (status.connection_state, status.enabled) == ("DISABLED", False)
    assert _count(factory, StreamSessionRecord) == 0


def test_unregistered_symbol_is_a_configuration_failure(factory):
    async def scenario():
        worker = _worker(
            factory, "ws://127.0.0.1:9/v2/iex", _settings(market_data_symbols="AAPL,MSFT")
        )
        return await worker.run(asyncio.Event())

    assert run(scenario()) == EXIT_CONFIGURATION
    status = _status(factory)
    assert status.connection_state == "FAILED"
    assert "MSFT" in (status.last_error or "")
    assert _count(factory, StreamSessionRecord) == 0  # never connected


def test_channel_not_offered_by_the_feed_is_refused(factory):
    async def scenario():
        worker = _worker(factory, "ws://127.0.0.1:9/v2/iex")
        worker._provider.streaming_channels = lambda: frozenset()  # type: ignore[method-assign]
        return await worker.run(asyncio.Event())

    assert run(scenario()) == EXIT_CONFIGURATION
    assert "not available" in (_status(factory).last_error or "")


# ------------------------------------------------------------------ end-to-end processing
def test_messages_are_normalized_validated_and_persisted(factory):
    frames = [
        frame(
            bar_msg("AAPL", MINUTE, kind="b", c=100.2),
            quote_msg("AAPL", "2026-01-02T14:30:01.000000123Z"),
            trade_msg("AAPL", "2026-01-02T14:30:01.123456789Z", i=1, p=100.123456789),
            trade_msg("SPY", "2026-01-02T14:30:01.5Z", i=1),
        ),
        # Late trades: Alpaca re-emits the 14:30 bar as an updated bar.
        frame(bar_msg("AAPL", MINUTE, kind="u", c=100.3, v=1300)),
        frame(
            trade_msg("AAPL", "2026-01-02T14:30:01.123456789Z", i=1, p=100.123456789),  # repeat
            quote_msg("SPY", "2026-01-02T14:30:02Z", bp=500.1, ap=500.0),  # crossed
            bar_msg("SPY", MINUTE, h=99, low=100),  # impossible OHLC
            trade_msg("MSFT", "2026-01-02T14:30:02Z"),  # not subscribed
            {"T": "t", "S": "AAPL", "i": 9, "t": "2026-01-02T14:30:02Z"},  # no price
        ),
        frame(
            {
                "T": "c",
                "S": "AAPL",
                "x": "V",
                "oi": 1,
                "op": 100.1,
                "os": 10,
                "ci": 2,
                "cp": 100.2,
                "cs": 10,
                "t": "2026-01-02T14:30:01.123456789Z",
                "z": "C",
            }
        ),
    ]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(factory, server.url)
            code = await _run_until(worker, lambda: worker.counters["frames"] >= 4)
            return code, worker, server.connections

    code, worker, connections = run(scenario())
    assert code == EXIT_OK
    (conn,) = connections
    assert conn.subscribe_messages == [
        {
            "action": "subscribe",
            "bars": ["AAPL", "SPY"],
            "quotes": ["AAPL", "SPY"],
            "trades": ["AAPL", "SPY"],
            "updatedBars": ["AAPL", "SPY"],
        }
    ]

    (session,) = _rows(factory, StreamSessionRecord)
    assert session.authenticated_at is not None
    assert session.subscribed_at is not None
    assert session.end_reason == "shutdown"
    assert session.counters["frames"] == 4

    # Bars: the first emission and its update are both kept, as revisions.
    aapl_bars = _rows(
        factory,
        MarketBarRecord,
        MarketBarRecord.provider_symbol == "AAPL",
        order=MarketBarRecord.revision,
    )
    assert [(b.revision, b.observation_kind, b.close) for b in aapl_bars] == [
        (0, BarObservationKind.LIVE_INITIAL, Decimal("100.2")),
        (1, BarObservationKind.LIVE_UPDATED, Decimal("100.3")),
    ]
    for bar in aapl_bars:
        assert bar.source is DataSource.LIVE_STREAM
        assert bar.feed == "iex"
        assert bar.adjustment is PriceAdjustment.RAW
        assert bar.event_time == MINUTE_DT
        assert bar.original_available_at == bar.received_at
        assert (
            bar.received_at
            <= bar.processed_at
            <= bar.available_at
            <= bar.ingested_at + timedelta(seconds=1)
        )
        assert bar.quality_status is DataQualityStatus.VALID
    (spy_bar,) = _rows(factory, MarketBarRecord, MarketBarRecord.provider_symbol == "SPY")
    assert spy_bar.quality_status is DataQualityStatus.QUARANTINED

    # Quotes and trades: exact values and nanosecond time, identical repeat not stored.
    quotes = _rows(factory, MarketQuoteRecord, order=MarketQuoteRecord.id)
    assert [(q.provider_symbol, q.quality_status) for q in quotes] == [
        ("AAPL", DataQualityStatus.VALID),
        ("SPY", DataQualityStatus.QUARANTINED),
    ]
    assert quotes[0].event_time_ns == 1767364201000000123
    trades = _rows(factory, MarketTradeRecord, order=MarketTradeRecord.id)
    assert [(t.provider_symbol, t.provider_trade_id) for t in trades] == [
        ("AAPL", "1"),
        ("SPY", "1"),
    ]
    assert trades[0].price == Decimal("100.123456789")
    assert trades[0].event_time_ns == 1767364201123456789
    assert trades[0].event_time == datetime(2026, 1, 2, 14, 30, 1, 123456, tzinfo=UTC)
    assert trades[0].feed == "iex"

    # Every frame is kept verbatim and linked to its connection.
    raws = _rows(factory, RawMarketDataEventRecord, order=RawMarketDataEventRecord.id)
    assert len(raws) == 4
    assert all(r.stream_session_id == session.id for r in raws)
    assert raws[1].payload_text == frames[1]
    assert {b.raw_event_id for b in aapl_bars} <= {r.id for r in raws}

    types = _event_types(factory)
    for expected in (
        DataQualityEventType.CORRECTION,  # bar update + trade correction
        DataQualityEventType.DUPLICATE_EVENT,
        DataQualityEventType.INVALID_QUOTE,
        DataQualityEventType.INVALID_OHLC,
        DataQualityEventType.UNEXPECTED_SYMBOL,
        DataQualityEventType.MISSING_FIELD,
    ):
        assert expected in types
    assert types.count(DataQualityEventType.CORRECTION) == 2

    counters = worker.counters
    assert counters["trades_stored"] == 2
    assert counters["trades_duplicate"] == 1
    assert counters["bars_revised"] == 1
    assert counters["records_rejected"] == 2
    assert counters["records_quarantined"] == 2
    assert counters["frames_dropped"] == 0

    status = _status(factory)
    assert status.connection_state == "STOPPED"
    assert status.worker_stopped_at is not None
    assert status.last_persisted_at is not None


def test_live_and_historical_observations_share_the_series_and_revision_rules(factory):
    """A live bar equal to the stored REST bar is a duplicate; a different one is a new
    revision, and what Atlas knew before the correction stays queryable."""
    with session_scope(factory) as session:
        asset_id = session.scalar(select(Asset.id).where(Asset.symbol == "AAPL"))
        retrieved = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
        session.execute(
            insert(MarketBarRecord),
            [
                {
                    "asset_id": asset_id,
                    "provider": "alpaca",
                    "feed": "iex",
                    "provider_symbol": "AAPL",
                    "timeframe": "1Min",
                    "adjustment": PriceAdjustment.RAW,
                    "event_time": MINUTE_DT,
                    "revision": 0,
                    "open": Decimal("100.1"),
                    "high": Decimal("100.5"),
                    "low": Decimal("99.9"),
                    "close": Decimal("100.2"),
                    "volume": Decimal("1200"),
                    "vwap": Decimal("100.21"),
                    "trade_count": 15,
                    "is_complete": True,
                    "source": DataSource.HISTORICAL_REST,
                    "observation_kind": BarObservationKind.HISTORICAL,
                    "quality_status": DataQualityStatus.VALID,
                    "received_at": retrieved,
                    "available_at": retrieved,
                    "processed_at": retrieved,
                }
            ],
        )
    before_live = datetime.now(UTC)
    time.sleep(0.05)
    frames = [
        frame(bar_msg("AAPL", MINUTE, kind="b")),  # identical to the REST bar
        frame(bar_msg("AAPL", MINUTE, kind="u", c=100.4, v=1500)),
    ]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(factory, server.url)
            await _run_until(worker, lambda: worker.counters["frames"] >= 2)
            return worker

    worker = run(scenario())
    assert worker.counters["bars_duplicate"] == 1
    bars = _rows(factory, MarketBarRecord, order=MarketBarRecord.revision)
    assert [(b.revision, b.source, b.close) for b in bars] == [
        (0, DataSource.HISTORICAL_REST, Decimal("100.2")),
        (1, DataSource.LIVE_STREAM, Decimal("100.4")),
    ]
    key = SeriesKey(bars[0].asset_id, "1Min", "alpaca", "iex", PriceAdjustment.RAW)
    with session_scope(factory) as session:
        repo = MarketBarRepository(session)
        statuses = [DataQualityStatus.VALID]
        (now_view,) = repo.current_bars(key, start=MINUTE_DT, end=MINUTE_DT, statuses=statuses)
        (past_view,) = repo.current_bars(
            key, start=MINUTE_DT, end=MINUTE_DT, statuses=statuses, known_at=before_live
        )
    assert now_view.close == Decimal("100.4")
    assert past_view.close == Decimal("100.2")  # the decision-time view is not rewritten


# ------------------------------------------------------------------ reliability
def test_reconnects_restores_subscription_and_records_the_gap(factory):
    first = [frame(trade_msg("AAPL", "2026-01-02T14:30:01Z", i=1))]
    second = [frame(trade_msg("AAPL", "2026-01-02T14:30:05Z", i=2))]

    async def scenario():
        script = Script(frames=[first, second], drop_after_frames=[True, False])
        async with FakeAlpacaStreamServer(script) as server:
            worker = _worker(factory, server.url)
            code = await _run_until(worker, lambda: worker.counters["trades_stored"] >= 2)
            return code, worker, server.connections

    code, worker, connections = run(scenario())
    assert code == EXIT_OK
    assert len(connections) == 2
    assert connections[0].subscribe_messages == connections[1].subscribe_messages  # restored
    assert all(len(c.auth_messages) == 1 for c in connections)
    assert worker.reconnect_count == 1

    sessions = _rows(factory, StreamSessionRecord, order=StreamSessionRecord.started_at)
    assert len(sessions) == 2
    assert "closed unexpectedly" in (sessions[0].end_reason or "")
    # Each session keeps its own timeline, even one that died before a heartbeat.
    assert sessions[0].authenticated_at is not None
    assert sessions[0].subscribed_at is not None
    assert sessions[0].ended_at is not None
    assert sessions[1].subscribed_at is not None

    (gap,) = _rows(factory, StreamGapRecord)
    assert gap.kind is StreamGapKind.CONNECTION_LOST
    assert gap.gap_end is not None
    assert gap.gap_end >= gap.gap_start
    assert gap.previous_session_id == sessions[0].id
    assert gap.recovery_session_id == sessions[1].id
    assert gap.recovery_status is GapRecoveryStatus.PENDING  # backfill is 2D
    assert gap.last_message_before is not None
    assert set(gap.symbols) == {"AAPL", "SPY"}
    types = _event_types(factory)
    assert DataQualityEventType.PROVIDER_OUTAGE in types
    assert DataQualityEventType.RECOVERY in types
    assert _status(factory).reconnect_count == 1


def test_rejected_credentials_fail_permanently_without_retrying(factory):
    async def scenario():
        script = Script(auth_errors=[(402, "auth failed")])
        async with FakeAlpacaStreamServer(script) as server:
            worker = _worker(factory, server.url)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(lambda: worker.state.value == "FAILED")
            await asyncio.sleep(0.5)  # well past several backoff periods
            status = _status(factory)
            stop.set()
            return await asyncio.wait_for(task, 10), len(server.connections), status

    code, connections, status = run(scenario())
    assert code == EXIT_STREAM_FAILED
    assert connections == 1
    assert (status.connection_state, status.auth_state) == ("FAILED", "REJECTED")
    assert "402" in (status.last_error or "")
    assert _status(factory).connection_state == "FAILED"


def test_connection_limit_error_is_retried(factory):
    async def scenario():
        script = Script(auth_errors=[(406, "connection limit exceeded"), None])
        async with FakeAlpacaStreamServer(script) as server:
            worker = _worker(factory, server.url)
            code = await _run_until(worker, lambda: worker.state.value == "SUBSCRIBED")
            return code, worker, len(server.connections)

    code, worker, connections = run(scenario())
    assert (code, connections, worker.reconnect_count) == (EXIT_OK, 2, 1)
    assert _count(factory, StreamGapRecord) == 0  # nothing was streaming yet: no data gap


def test_only_one_worker_per_provider_feed(factory):
    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            first = _worker(factory, server.url)
            stop = asyncio.Event()
            task = asyncio.create_task(first.run(stop))
            await eventually(lambda: first.state.value == "SUBSCRIBED")
            second_code = await _worker(factory, server.url).run(asyncio.Event())
            stop.set()
            first_code = await asyncio.wait_for(task, 10)
            # The lock is released on shutdown: a new worker can start.
            third = _worker(factory, server.url)
            third_code = await _run_until(third, lambda: third.state.value == "SUBSCRIBED")
            return first_code, second_code, third_code, len(server.connections)

    assert run(scenario()) == (EXIT_OK, EXIT_ALREADY_RUNNING, EXIT_OK, 2)


def test_previous_crash_is_closed_and_recorded_as_a_gap(factory):
    crashed_at = datetime.now(UTC) - timedelta(minutes=5)
    with session_scope(factory) as session:
        session.add(
            StreamSessionRecord(
                provider="alpaca",
                feed="iex",
                worker_id="crashed-worker",
                state="SUBSCRIBED",
                url="wss://example.invalid",
                started_at=crashed_at - timedelta(minutes=10),
                last_message_at=crashed_at,
            )
        )

    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            worker = _worker(factory, server.url)
            return await _run_until(worker, lambda: worker.state.value == "SUBSCRIBED")

    assert run(scenario()) == EXIT_OK
    crashed = _rows(factory, StreamSessionRecord, StreamSessionRecord.worker_id == "crashed-worker")
    assert crashed[0].ended_at is not None
    assert crashed[0].end_reason == INTERRUPTED_REASON
    (gap,) = _rows(factory, StreamGapRecord)
    assert gap.kind is StreamGapKind.WORKER_NOT_RUNNING
    assert gap.gap_start == crashed_at
    assert gap.gap_end is not None  # closed when the new subscription was confirmed
    assert DataQualityEventType.RECOVERY in _event_types(factory)


def test_temporary_database_failure_is_retried_without_loss(factory):
    frames = [frame(trade_msg("AAPL", f"2026-01-02T14:30:0{i}Z", i=i)) for i in range(3)]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(factory, server.url)
            real_persist = worker._live.persist
            failures = {"left": 2}

            def flaky(batches, session_id, frame_seqs=None):
                if failures["left"]:
                    failures["left"] -= 1
                    raise OperationalError("INSERT", {}, Exception("server closed the connection"))
                return real_persist(batches, session_id, frame_seqs)

            worker._live.persist = flaky  # type: ignore[method-assign]
            await _run_until(worker, lambda: worker.counters["trades_stored"] >= 3)
            return worker

    worker = run(scenario())
    assert worker.counters["persist_failures"] == 2
    assert worker.counters["frames_dropped"] == 0
    assert _count(factory, MarketTradeRecord) == 3
    assert _count(factory, RawMarketDataEventRecord) == 3  # rolled-back attempts left nothing
    assert any(e["kind"] == "database" for e in _status(factory).recent_errors)
    # 2D: the interruption is counted once and audited when writes succeed again.
    assert worker.counters["database_interruptions"] == 1
    with session_scope(factory) as session:
        actions = set(session.scalars(select(AuditLog.action)))
    assert {"market_data.database_failure", "market_data.persistence_recovered"} <= actions


def test_backpressure_drops_are_recorded_never_silent(factory):
    sent = 40
    frames = [frame(trade_msg("AAPL", f"2026-01-02T14:31:{i:02d}Z", i=i)) for i in range(sent)]
    release = threading.Event()

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(
                factory,
                server.url,
                _settings(market_data_stream_queue_size=10),
                enqueue_timeout=0.01,
                flush_max_frames=5,
            )
            real_persist = worker._live.persist

            def slow(batches, session_id, frame_seqs=None):
                release.wait(10)
                return real_persist(batches, session_id, frame_seqs)

            worker._live.persist = slow  # type: ignore[method-assign]
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(lambda: worker.counters["frames_received"] >= sent)
            release.set()
            await eventually(
                lambda: worker.counters["frames"] + worker.counters["frames_dropped"] >= sent
            )
            await asyncio.sleep(0.3)  # heartbeat records the drop interval
            stop.set()
            await asyncio.wait_for(task, 10)
            return worker

    worker = run(scenario())
    dropped = worker.counters["frames_dropped"]
    assert dropped > 0
    assert worker.counters["frames"] + dropped == sent  # every frame accounted for
    assert _count(factory, MarketTradeRecord) == sent - dropped
    # One gap per heartbeat interval in which drops happened; together they cover all.
    gaps = _rows(factory, StreamGapRecord)
    assert gaps
    assert {g.kind for g in gaps} == {StreamGapKind.DATA_DROPPED}
    assert sum(g.frames_dropped for g in gaps) == dropped
    assert all(g.gap_end is not None for g in gaps)
    assert DataQualityEventType.DATA_DROPPED in _event_types(factory)
    assert _status(factory).metrics["last_drop_at"] is not None


# ------------------------------------------------------------------ API while streaming
class _WorkerThread:
    """Runs a fake server and a worker on their own event loop, like a separate process."""

    def __init__(self, factory, script: Script) -> None:
        self._factory = factory
        self._script = script
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.worker: StreamWorker | None = None
        self.exit_code: int | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with FakeAlpacaStreamServer(self._script) as server:
            self.worker = _worker(self._factory, server.url)
            self.exit_code = await self.worker.run(self._stop)

    def __enter__(self) -> _WorkerThread:
        self._thread.start()
        deadline = time.monotonic() + 10
        while self.worker is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(20)
        assert not self._thread.is_alive()


def test_api_reports_stream_status_and_stays_responsive(factory, make_settings):
    frames = [
        frame(
            trade_msg("AAPL", f"2026-01-02T14:32:{i:02d}.000000001Z", i=i),
            quote_msg("SPY", f"2026-01-02T14:32:{i:02d}.5Z"),
        )
        for i in range(30)
    ]
    script = Script(frames=[frames], frame_delay=0.05)
    # The API's own configuration has streaming disabled while the worker runs enabled (an
    # override in the worker's environment): `enabled` must report the worker's view.
    md_settings = _settings(market_data_stream_enabled=False)
    provider = _provider("ws://unused")
    context = MarketDataContext(
        settings=md_settings,
        provider=provider,
        service=HistoricalIngestionService(factory, provider, md_settings, SystemClock()),
        session_factory=factory,
    )
    app = create_app(make_settings(), clock=SystemClock(), health_checks={}, market_data=context)

    with TestClient(app) as client:
        before = client.get("/api/v1/market-data/stream").json()
        assert before["health"] == StreamHealth.DISABLED
        assert before["enabled"] is False
        with _WorkerThread(factory, script) as running:
            durations = []
            deadline = time.monotonic() + 15
            body = {}
            while time.monotonic() < deadline:
                started = time.monotonic()
                response = client.get("/api/v1/market-data/stream")
                durations.append(time.monotonic() - started)
                body = response.json()
                if running.worker and running.worker.counters["frames"] >= 30:
                    break
                time.sleep(0.05)
            body = client.get("/api/v1/market-data/stream").json()
            sessions = client.get("/api/v1/market-data/stream/sessions").json()
            trades = client.get(
                "/api/v1/market-data/trades",
                params={
                    "symbol": "AAPL",
                    "start": "2026-01-02T14:00:00Z",
                    "end": "2026-01-02T15:00:00Z",
                },
            ).json()
            quotes = client.get(
                "/api/v1/market-data/quotes",
                params={
                    "symbol": "SPY",
                    "start": "2026-01-02T14:00:00Z",
                    "end": "2026-01-02T15:00:00Z",
                },
            ).json()
        after = client.get("/api/v1/market-data/stream").json()

    assert max(durations) < 2.0  # the API never waits on the stream
    assert body["health"] == StreamHealth.HEALTHY
    assert body["enabled"] is True
    assert body["connection_state"] == "SUBSCRIBED"
    assert body["auth_state"] == "AUTHENTICATED"
    assert body["subscription_state"] == "ACTIVE"
    assert body["subscribed_symbols"] == ["AAPL", "SPY"]
    assert body["active_channels"] == ["bars", "quotes", "trades", "updated_bars"]
    assert body["feed"] == "iex"
    assert body["feed_coverage"] == "SINGLE_VENUE"
    assert body["last_message_at"]
    assert body["last_persisted_at"]
    assert body["counters"]["trades_stored"] >= 1
    assert body["metrics"]["commit_latency_seconds"]["count"] >= 1
    assert FAKE_STREAM_SECRET not in str(body)
    assert FAKE_STREAM_KEY not in str(body)
    assert sessions[0]["subscribed_at"] is not None
    assert len(trades["trades"]) == 30
    assert trades["trades"][0]["event_time_ns"] == 1767364320000000001
    assert trades["feed_coverage"] == "SINGLE_VENUE"
    assert len(quotes["quotes"]) == 30
    assert after["health"] == StreamHealth.NOT_RUNNING
    assert after["connection_state"] == "STOPPED"
    assert after["subscribed_symbols"] == []  # a stopped stream subscribes nothing
    assert running.exit_code == EXIT_OK


# ------------------------------------------------------------------ process entry point
def test_process_entry_point_respects_disabled_streaming(fresh_engine, clean_env, tmp_path):
    """``python -m app.workers.market_data_stream`` with streaming disabled exits 0."""
    from app.tests.conftest import _clear_settings_caches
    from app.workers.market_data_stream import main

    url = fresh_engine.url
    clean_env.chdir(tmp_path)  # never read the developer's real .env
    for key, value in {
        "ATLAS_ENVIRONMENT": "test",
        "ATLAS_POSTGRES_HOST": url.host,
        "ATLAS_POSTGRES_PORT": str(url.port),
        "ATLAS_POSTGRES_DB": url.database,
        "ATLAS_POSTGRES_USER": url.username,
        "ATLAS_POSTGRES_PASSWORD": url.password,
        "ATLAS_REDIS_URL": "redis://127.0.0.1:6379/15",
        "MARKET_DATA_STREAM_ENABLED": "false",
    }.items():
        clean_env.setenv(key, str(value))

    assert main(["--duration", "5"]) == EXIT_OK
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    assert _status(factory).connection_state == "DISABLED"

    clean_env.setenv("MARKET_DATA_STREAM_TYPO", "1")
    _clear_settings_caches()  # settings are loaded once per process
    assert main([]) == EXIT_CONFIGURATION  # unknown keys are rejected, values never echoed


# ------------------------------------------------------------------ Deliverable 2D
def test_group_retried_after_a_lost_commit_acknowledgement_is_not_stored_twice(factory):
    """MOCK: the commit succeeds but its acknowledgement is lost; the retry must not
    write the frames again (frame sequence numbers identify them)."""
    frames = [frame(trade_msg("AAPL", f"2026-01-02T14:30:0{i}Z", i=i)) for i in range(3)]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(factory, server.url)
            real_persist = worker._live.persist
            lost = {"left": 1}

            def lost_ack(batches, session_id, frame_seqs=None):
                result = real_persist(batches, session_id, frame_seqs)
                if lost["left"]:
                    lost["left"] -= 1
                    raise OperationalError("COMMIT", {}, Exception("connection lost after commit"))
                return result

            worker._live.persist = lost_ack  # type: ignore[method-assign]
            await _run_until(
                worker,
                lambda: (
                    worker.counters["replays_suppressed"] >= 1
                    and _count(factory, MarketTradeRecord) >= 3
                ),
            )
            return worker

    worker = run(scenario())
    assert worker.counters["replays_suppressed"] >= 1
    assert _count(factory, MarketTradeRecord) == 3
    assert _count(factory, RawMarketDataEventRecord) == 3  # each frame stored exactly once
    with session_scope(factory) as session:
        seqs = list(session.scalars(select(RawMarketDataEventRecord.frame_seq)))
    assert sorted(seq or 0 for seq in seqs) == [1, 2, 3]


def test_restart_records_frames_the_previous_worker_received_but_never_wrote(factory):
    """MOCK records: a previous session received 10 frames by its last heartbeat but only
    frames 1-6 reached the database (the process died with the rest in memory)."""
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    with session_scope(factory) as session:
        previous = StreamSessionRecord(
            provider="alpaca",
            feed="iex",
            worker_id="crashed-worker",
            state="SUBSCRIBED",
            url="wss://test",
            started_at=start,
            subscribed_at=start,
            last_message_at=start + timedelta(seconds=8),
            counters={"frames_received": 10, "frames": 6},
        )
        session.add(previous)
        session.flush()
        for seq in range(1, 7):
            session.add(
                RawMarketDataEventRecord(
                    provider="alpaca",
                    feed="iex",
                    source=DataSource.LIVE_STREAM,
                    message_type="frame",
                    stream_session_id=previous.id,
                    frame_seq=seq,
                    received_at=start + timedelta(seconds=seq),
                    payload_text="[]",
                    payload_sha256="0" * 64,
                )
            )
    worker = _worker(factory, "ws://127.0.0.1:9/v2/iex")
    worker._record_previous_run()
    with session_scope(factory) as session:
        gaps = {g.kind: g for g in session.scalars(select(StreamGapRecord))}
        events = session.scalars(
            select(DataQualityEventRecord).where(
                DataQualityEventRecord.event_type == DataQualityEventType.DATA_NOT_PERSISTED
            )
        ).all()
    lost = gaps[StreamGapKind.DATA_NOT_PERSISTED]
    assert lost.frames_dropped == 4
    assert lost.gap_start == start + timedelta(seconds=6)  # the last frame written
    assert lost.gap_end is not None
    assert lost.gap_end > start + timedelta(seconds=8)
    assert "4 frame(s) never stored" in lost.reason
    assert StreamGapKind.WORKER_NOT_RUNNING in gaps
    assert len(events) == 1
    assert events[0].details["frames_lost"] == 4


def test_live_coverage_is_recorded_only_for_what_was_persisted(factory):
    frames = [
        frame(
            trade_msg("AAPL", f"2026-01-02T14:31:{i:02d}.000000001Z", i=i),
            quote_msg("SPY", f"2026-01-02T14:31:{i:02d}.5Z"),
        )
        for i in range(5)
    ]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(factory, server.url)
            await _run_until(
                worker,
                lambda: (
                    _count(
                        factory,
                        CoverageRecord,
                        CoverageRecord.evidence == CoverageEvidence.LIVE_STREAM,
                        CoverageRecord.data_type == DataType.QUOTE,
                    )
                    >= 2
                ),
            )
            return worker

    worker = run(scenario())
    with session_scope(factory) as session:
        rows = session.scalars(select(CoverageRecord)).all()
        session_ids = {r.stream_session_id for r in rows}
    assert {r.data_type for r in rows} == {DataType.TRADE, DataType.QUOTE}
    # Minute bars count only once a bar published after the subscription has arrived.
    assert all(r.data_type is not DataType.BAR for r in rows)
    assert len(session_ids) == 1
    assert all(r.covered_end > r.covered_start for r in rows)
    assert worker.counters["trades_stored"] == 5


def test_late_delivered_quotes_are_stored_stale(factory):
    frames = [frame(quote_msg("SPY", f"2026-01-02T14:31:0{i}.5Z")) for i in range(3)]

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
            worker = _worker(
                factory, server.url, _settings(market_data_stale_observation_seconds=60)
            )
            await _run_until(worker, lambda: worker.counters["quotes_stored"] >= 3)
            return worker

    worker = run(scenario())
    # Fixture quotes are dated 2026-01-02 and replayed now: far older than 60 seconds.
    assert worker.counters["stale"] == 3
    assert (
        _count(
            factory, MarketQuoteRecord, MarketQuoteRecord.quality_status == DataQualityStatus.STALE
        )
        == 3
    )
    assert (
        _count(
            factory,
            DataQualityEventRecord,
            DataQualityEventRecord.event_type == DataQualityEventType.STALE_DATA,
        )
        >= 1
    )

"""Dashboard API (Deliverable 2E) over real PostgreSQL.

Market observations here are MOCK rows written by the tests (labelled with the
``MOCK_`` helpers) or produced by the real stream worker from the FAKE Alpaca stream
server. Nothing is real market data. Provider calls are never made by these endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import sessionmaker

from app.api.v1.market_data_live import _resolve
from app.api.v1.market_data_overview import MAX_SNAPSHOT_SYMBOLS
from app.core.clocks import FixedClock, SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.data_engine.historical import HistoricalFetchRequest, HistoricalIngestionService
from app.data_engine.live_updates import LiveUpdateHub
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.runtime import MarketDataContext
from app.database.models import (
    Asset,
    MarketBarRecord,
    MarketQuoteRecord,
    MarketTradeRecord,
)
from app.database.models.audit_log import ActorType
from app.database.repositories import AuditLogRepository, StreamRepository
from app.database.sessions import session_scope
from app.main import create_app
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, FakeAlpaca, fake_provider
from app.tests.fixtures.alpaca_stream import FakeAlpacaStreamServer, Script, bar_msg, frame

pytestmark = pytest.mark.integration

# Tuesday 10 March 2026, 11:00 EDT: regular session, IEX publishing.
NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
PREVIOUS_DAY_BAR = datetime(2026, 3, 9, 4, 0, tzinfo=UTC)  # Mon 9 Mar, New York midnight
BASE = "/api/v1/market-data"


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    return factory


def _asset_id(factory, symbol: str) -> uuid.UUID:
    with session_scope(factory) as session:
        return session.scalars(select(Asset.id).where(Asset.symbol == symbol)).one()


def _context(factory, now: datetime, **settings: Any) -> MarketDataContext:
    # The registered assets are the monitored ones (MARKET_DATA_SYMBOLS), as in a real setup.
    settings.setdefault("market_data_symbols", ("AAPL", "SPY"))
    md_settings = MarketDataSettings(_env_file=None, **settings)
    provider = fake_provider(FakeAlpaca(), now=now)
    return MarketDataContext(
        settings=md_settings,
        provider=provider,
        service=HistoricalIngestionService(factory, provider, md_settings, FixedClock(now)),
        session_factory=factory,
    )


@pytest.fixture
def make_client(factory, make_settings):
    def build(
        now: datetime = NOW, monitored: tuple[str, ...] = ("AAPL", "SPY"), **settings_overrides: Any
    ) -> TestClient:
        app = create_app(
            make_settings(**settings_overrides),
            clock=FixedClock(now),
            health_checks={},
            market_data=_context(factory, now, market_data_symbols=monitored),
        )
        return TestClient(app)

    return build


# ------------------------------------------------------------------ MOCK observations
def mock_bar(factory, symbol: str, timeframe: str, at: datetime, close: str, **extra: Any) -> None:
    """Insert a MOCK bar row (test data, not market data)."""
    row = {
        "asset_id": _asset_id(factory, symbol),
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": symbol,
        "timeframe": timeframe,
        "adjustment": PriceAdjustment.RAW,
        "event_time": at,
        "revision": 0,
        "open": Decimal(close),
        "high": Decimal(close) + 1,
        "low": Decimal(close) - 1,
        "close": Decimal(close),
        "volume": Decimal(1000),
        "vwap": None,
        "trade_count": 10,
        "is_complete": True,
        "source": DataSource.HISTORICAL_REST,
        "observation_kind": BarObservationKind.HISTORICAL,
        "quality_status": DataQualityStatus.VALID,
        "received_at": at,
        "available_at": at,
        "original_available_at": None,
        "processed_at": at,
    } | extra
    with session_scope(factory) as session:
        session.execute(insert(MarketBarRecord), [row])


def _observation(factory, symbol: str, at: datetime, source: DataSource) -> dict[str, Any]:
    ns = int(at.timestamp() * 1_000_000) * 1000
    return {
        "asset_id": _asset_id(factory, symbol),
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": symbol,
        "event_time": at,
        "event_time_ns": ns,
        "source": source,
        "quality_status": DataQualityStatus.VALID,
        "content_sha256": hashlib.sha256(f"{symbol}{ns}{uuid.uuid4()}".encode()).hexdigest(),
        "received_at": at,
        "available_at": at,
        "original_available_at": None,
        "processed_at": at,
    }


def mock_trade(
    factory, symbol: str, at: datetime, price: str, source=DataSource.LIVE_STREAM
) -> None:
    row = _observation(factory, symbol, at, source) | {"price": Decimal(price), "size": Decimal(5)}
    with session_scope(factory) as session:
        session.execute(insert(MarketTradeRecord), [row])


def mock_quote(factory, symbol: str, at: datetime, bid: str, ask: str) -> None:
    row = _observation(factory, symbol, at, DataSource.LIVE_STREAM) | {
        "bid_price": Decimal(bid),
        "ask_price": Decimal(ask),
        "bid_size": Decimal(1),
        "ask_size": Decimal(2),
    }
    with session_scope(factory) as session:
        session.execute(insert(MarketQuoteRecord), [row])


def _stream_status(factory, **values: Any) -> None:
    """A stream-worker status row (MOCK operational state)."""
    row = {
        "worker_id": "w",
        "enabled": True,
        "connection_state": "SUBSCRIBED",
        "auth_state": "AUTHENTICATED",
        "subscription_state": "ACTIVE",
        "requested_subscription": {"trades": ["AAPL", "SPY"]},
        "confirmed_subscription": {"trades": ["AAPL", "SPY"]},
        "heartbeat_at": NOW,
        "metrics": {},
    } | values
    with session_scope(factory) as session:
        StreamRepository(session).upsert_status("alpaca", "iex", row)


def _snapshot(client: TestClient, symbol: str) -> dict[str, Any]:
    body = client.get(f"{BASE}/snapshots").json()
    return next(s for s in body["snapshots"] if s["asset"]["symbol"] == symbol)


# ------------------------------------------------------------------ market status
def test_market_status_comes_from_the_backend_calendar(make_client):
    with make_client() as client:
        body = client.get(f"{BASE}/market-status").json()
    assert body["session"] == "REGULAR"
    assert body["is_regular_session"] is True
    assert body["feed"] == "iex"
    assert body["feed_open"] is True
    assert body["next_regular_close"] == "2026-03-10T20:00:00Z"
    with make_client(datetime(2026, 3, 14, 15, tzinfo=UTC)) as client:  # Saturday
        weekend = client.get(f"{BASE}/market-status").json()
    assert weekend["session"] == "CLOSED"
    assert weekend["closure"] == "weekend"
    assert weekend["next_regular_open"] == "2026-03-16T13:30:00Z"


# ------------------------------------------------------------------ snapshots
def test_snapshot_uses_stored_trades_bars_and_the_previous_daily_close(factory, make_client):
    mock_bar(factory, "AAPL", "1Day", PREVIOUS_DAY_BAR, "100.00")
    mock_bar(factory, "AAPL", "1Min", NOW - timedelta(minutes=2), "101.00")
    mock_trade(factory, "AAPL", NOW - timedelta(seconds=30), "101.50")
    mock_quote(factory, "AAPL", NOW - timedelta(seconds=15), "101.45", "101.55")
    with make_client() as client:
        aapl = _snapshot(client, "AAPL")
        spy = _snapshot(client, "SPY")
    assert aapl["asset"]["name"] == "Apple Inc."
    assert aapl["last_price"]["value"] == "101.50"
    assert aapl["last_price"]["basis"] == "TRADE"
    assert aapl["last_price"]["source"] == "LIVE_STREAM"
    assert aapl["previous_close"]["value"] == "100.00"
    assert aapl["previous_close"]["session_date"] == "2026-03-09"
    assert "not the official consolidated closing price" in aapl["previous_close"]["basis"]
    assert Decimal(aapl["change"]) == Decimal("1.50")
    assert aapl["change_percent"] == "1.5000"
    assert aapl["latest_quote"]["bid_price"] == "101.45"
    assert aapl["latest_event_at"] == (NOW - timedelta(seconds=15)).isoformat().replace(
        "+00:00", "Z"
    )
    assert aapl["freshness"] == "LIVE"
    assert aapl["feed"] == "iex"
    assert aapl["feed_coverage"] == "SINGLE_VENUE"
    # Nothing stored for SPY: nothing invented.
    assert spy["last_price"] is None
    assert spy["previous_close"] is None
    assert spy["change"] is None
    assert spy["freshness"] == "NO_DATA"
    assert spy["previous_close_note"] == "no stored trades or 1-minute bars"


def test_snapshot_without_a_valid_previous_close_shows_no_change(factory, make_client):
    mock_bar(factory, "AAPL", "1Min", NOW - timedelta(minutes=1), "101.00")
    with make_client() as client:
        missing = _snapshot(client, "AAPL")
    assert missing["last_price"]["basis"] == "BAR_CLOSE"
    assert missing["last_price"]["source"] == "HISTORICAL_REST"
    assert missing["freshness"] == "HISTORICAL"
    assert missing["previous_close"] is None
    assert missing["change"] is None
    assert missing["change_percent"] is None
    assert "no stored iex daily bar for 2026-03-09" in missing["previous_close_note"]
    mock_bar(
        factory, "AAPL", "1Day", PREVIOUS_DAY_BAR, "100.00",
        quality_status=DataQualityStatus.INCOMPLETE, is_complete=False,
    )  # fmt: skip
    with make_client() as client:
        incomplete = _snapshot(client, "AAPL")
    assert incomplete["previous_close"] is None
    assert "INCOMPLETE" in incomplete["previous_close_note"]


def test_snapshot_freshness_follows_the_session_and_the_threshold(factory, make_client):
    mock_trade(factory, "AAPL", NOW - timedelta(seconds=30), "101.50")
    with make_client(NOW + timedelta(minutes=10)) as client:
        assert _snapshot(client, "AAPL")["freshness"] == "STALE"
    with make_client(datetime(2026, 3, 14, 15, tzinfo=UTC)) as client:
        weekend = _snapshot(client, "AAPL")
    assert weekend["freshness"] == "MARKET_CLOSED"
    assert weekend["last_price"]["value"] == "101.50"  # last session's value, labelled


def test_snapshot_symbol_filter_and_subscription_flag(factory, make_client):
    _stream_status(factory, confirmed_subscription={"trades": ["SPY"]})
    with make_client() as client:
        body = client.get(f"{BASE}/snapshots", params={"symbol": "spy"}).json()
    assert [s["asset"]["symbol"] for s in body["snapshots"]] == ["SPY"]
    assert body["snapshots"][0]["subscribed"] is True
    assert body["market"]["session"] == "REGULAR"


def test_snapshots_cover_monitored_assets_not_every_mapped_asset(factory, make_client):
    # Since Phase 3A discovery maps the whole provider directory; snapshotting all of it
    # exhausted the API's connection pool. Here SPY is mapped but not configured.
    with make_client(monitored=("AAPL",)) as client:
        configured = client.get(f"{BASE}/snapshots").json()
        explicit = client.get(f"{BASE}/snapshots", params={"symbol": "SPY"}).json()
    assert [s["asset"]["symbol"] for s in configured["snapshots"]] == ["AAPL"]
    assert [s["asset"]["symbol"] for s in explicit["snapshots"]] == ["SPY"]

    # The stream worker may run with its own environment: its symbols are monitored too.
    _stream_status(factory, requested_subscription={"trades": ["SPY"], "quotes": ["SPY"]})
    with make_client(monitored=("AAPL",)) as client:
        body = client.get(f"{BASE}/snapshots").json()
    assert [s["asset"]["symbol"] for s in body["snapshots"]] == ["AAPL", "SPY"]


def test_nothing_is_monitored_without_configured_or_streamed_symbols(make_client):
    with make_client(monitored=()) as client:
        body = client.get(f"{BASE}/snapshots").json()
    assert body["snapshots"] == []


def test_snapshots_refuse_more_symbols_than_the_bound(make_client):
    symbols = [f"S{i}" for i in range(MAX_SNAPSHOT_SYMBOLS + 1)]
    with make_client() as client:
        response = client.get(f"{BASE}/snapshots", params={"symbol": symbols})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_live_defaults_to_the_monitored_assets(factory):
    ctx = _context(factory, NOW, market_data_symbols=("SPY",))
    assert set(_resolve(ctx, None).values()) == {"SPY"}
    assert set(_resolve(ctx, ["aapl"]).values()) == {"AAPL"}  # explicit: any mapped asset


# ------------------------------------------------------------------ configuration
def test_configuration_reports_status_never_secret_values(make_client):
    with make_client(operator_token="operator-token-for-config-test") as client:
        response = client.get(f"{BASE}/configuration")
    assert response.status_code == 200
    text = response.text
    for secret in (FAKE_KEY, FAKE_SECRET, "operator-token-for-config-test"):
        assert secret not in text
    body = response.json()
    assert body["credentials"] == "CONFIGURED"
    assert body["operator_token_configured"] is True
    assert body["trading_mode"] == "PAPER"
    assert body["feed"] == "iex"
    assert body["stream_bar_timeframes"] == ["1Day", "1Min"]
    assert body["dashboard_timeframes"] == ["1Min", "5Min", "15Min", "1Hour", "1Day"]
    assert any("quotes" in note for note in body["limitations"])


# ------------------------------------------------------------------ audit trail
def test_audit_events_are_market_data_only_paged_and_filterable(factory, make_client):
    with session_scope(factory) as session:
        repo = AuditLogRepository(session)
        for i, priority in enumerate(("NORMAL", "HIGH", "NORMAL")):
            repo.append(
                actor_type=ActorType.SUBSYSTEM,
                actor_id="test",
                action=f"market_data.event_{i}",
                details={"priority": priority},
            )
        repo.append(actor_type=ActorType.SYSTEM, actor_id="test", action="other.event")
    with make_client() as client:
        everything = client.get(f"{BASE}/audit-events").json()
        high = client.get(f"{BASE}/audit-events", params={"priority": "HIGH"}).json()
        page = client.get(f"{BASE}/audit-events", params={"limit": 1}).json()
        older = client.get(f"{BASE}/audit-events", params={"before_id": page[0]["id"]}).json()
        bad = client.get(f"{BASE}/audit-events", params={"priority": "URGENT"})
    assert [e["action"] for e in everything] == [
        "market_data.event_2",
        "market_data.event_1",
        "market_data.event_0",
    ]
    assert [e["action"] for e in high] == ["market_data.event_1"]
    assert [e["action"] for e in older] == ["market_data.event_1", "market_data.event_0"]
    assert bad.status_code == 422


# ------------------------------------------------------------------ ingestion job filters
def test_ingestion_jobs_filter_by_asset_timeframe_and_creation_time(factory, make_client):
    with make_client() as client:
        service = client.app.state.market_data.service
        for symbol, timeframe in (("AAPL", "1Min"), ("SPY", "1Day")):
            service.create_job(
                HistoricalFetchRequest(
                    symbols=(symbol,),
                    timeframe=timeframe,
                    start=NOW - timedelta(days=2),
                    end=NOW - timedelta(days=1),
                ),
                requested_by="test",
            )
        jobs = client.get(f"{BASE}/ingestion-jobs").json()
        aapl = client.get(f"{BASE}/ingestion-jobs", params={"symbol": "aapl"}).json()
        daily = client.get(f"{BASE}/ingestion-jobs", params={"timeframe": "1Day"}).json()
        future = client.get(
            f"{BASE}/ingestion-jobs", params={"created_from": "2099-01-01T00:00:00Z"}
        ).json()
        bad_tf = client.get(f"{BASE}/ingestion-jobs", params={"timeframe": "7Sec"})
        unknown = client.get(f"{BASE}/ingestion-jobs", params={"symbol": "NOPE"})
        reversed_range = client.get(
            f"{BASE}/ingestion-jobs",
            params={"created_from": "2026-03-02T00:00:00Z", "created_to": "2026-03-01T00:00:00Z"},
        )
    assert len(jobs) == 2
    assert [j["provider_symbols"] for j in aapl] == [["AAPL"]]
    assert [j["timeframe"] for j in daily] == ["1Day"]
    assert future == []
    assert bad_tf.status_code == 422
    assert unknown.status_code == 404
    assert reversed_range.status_code == 422


# ------------------------------------------------------------------ trades newest first
def test_trades_newest_first_returns_the_latest_rows(factory, make_client):
    for i in range(5):
        mock_trade(factory, "AAPL", NOW - timedelta(minutes=5 - i), f"10{i}.00")
    params = {
        "symbol": "AAPL",
        "start": (NOW - timedelta(hours=1)).isoformat(),
        "end": NOW.isoformat(),
        "limit": 2,
    }
    with make_client() as client:
        newest = client.get(f"{BASE}/trades", params=params | {"newest_first": True}).json()
        oldest = client.get(f"{BASE}/trades", params=params).json()
        conflict = client.get(
            f"{BASE}/trades", params=params | {"newest_first": True, "after_id": 1}
        )
    assert [t["price"] for t in newest["trades"]] == ["104.00", "103.00"]
    assert newest["next_after_id"] is None
    assert [t["price"] for t in oldest["trades"]] == ["100.00", "101.00"]
    assert conflict.status_code == 422


# ------------------------------------------------------------------ pipeline health (2E fix)
def test_pipeline_health_does_not_report_a_stopped_stream_as_authenticated(factory, make_client):
    _stream_status(
        factory,
        connection_state="STOPPED",
        heartbeat_at=NOW - timedelta(minutes=30),
        last_authenticated_at=NOW - timedelta(hours=1),
        last_message_at=NOW - timedelta(minutes=30),
        last_persisted_at=NOW - timedelta(minutes=30),
    )
    with make_client() as client:
        body = client.get(f"{BASE}/pipeline-health").json()
    dims = {d["name"]: d for d in body["dimensions"]}
    assert body["stream_health"] == "NOT_RUNNING"
    assert dims["authentication"]["state"] == "UNKNOWN"
    assert "last known auth state AUTHENTICATED" in dims["authentication"]["detail"]
    assert dims["subscription"]["state"] == "FAILING"
    assert dims["authentication"]["last_success_at"] == "2026-03-10T14:00:00Z"
    assert dims["database_persistence"]["last_success_at"] == "2026-03-10T14:30:00Z"
    assert body["pipeline_health"] == "FAILING"


# ------------------------------------------------------------------ database failures
def test_read_endpoints_answer_503_when_the_database_is_unreachable(make_settings):
    dead = sessionmaker(
        bind=create_engine(
            "postgresql+psycopg://atlas:x@127.0.0.1:1/atlas", connect_args={"connect_timeout": 2}
        )
    )
    app = create_app(
        make_settings(), clock=FixedClock(NOW), health_checks={}, market_data=_context(dead, NOW)
    )
    with TestClient(app) as client:
        snapshots = client.get(f"{BASE}/snapshots")
        status = client.get(f"{BASE}/market-status")  # calendar only: no database needed
        live = client.get(f"{BASE}/live")
    assert snapshots.status_code == 503
    assert snapshots.json()["error"]["code"] == "dependency_unavailable"
    assert status.status_code == 200
    assert live.status_code == 503


def test_live_rejects_unmonitored_symbols(make_client):
    with make_client() as client:
        response = client.get(f"{BASE}/live", params={"symbol": "NOPE"})
    assert response.status_code == 404
    assert "NOPE" in response.json()["error"]["message"]


# ------------------------------------------------------------------ live update hub
def test_hub_distributes_new_rows_only_and_revisions(factory):
    aapl, spy = _asset_id(factory, "AAPL"), _asset_id(factory, "SPY")
    mock_bar(factory, "AAPL", "1Min", NOW - timedelta(minutes=10), "99.00")  # history

    async def scenario() -> tuple[list[Any], list[Any], LiveUpdateHub]:
        hub = LiveUpdateHub(factory, feed="iex", clock=FixedClock(NOW), poll_interval=3600)
        sub_aapl = await hub.subscribe({aapl: "AAPL"})
        sub_spy = await hub.subscribe({spy: "SPY"})
        await hub.poll_once()  # nothing new yet: history is not replayed
        mock_bar(factory, "AAPL", "1Min", NOW - timedelta(minutes=1), "101.00")
        mock_bar(
            factory, "AAPL", "1Min", NOW - timedelta(minutes=1), "101.20",
            revision=1, observation_kind=BarObservationKind.LIVE_UPDATED,
            source=DataSource.LIVE_STREAM,
        )  # fmt: skip
        for i in range(3):
            mock_trade(factory, "AAPL", NOW - timedelta(seconds=10 - i), f"101.3{i}")
        mock_quote(factory, "SPY", NOW, "500.00", "500.10")
        await hub.poll_once()
        await hub.poll_once()  # idempotent: nothing is sent twice
        events_aapl = [sub_aapl.queue.get_nowait() for _ in range(sub_aapl.queue.qsize())]
        events_spy = [sub_spy.queue.get_nowait() for _ in range(sub_spy.queue.qsize())]
        hub.unsubscribe(sub_aapl)
        hub.unsubscribe(sub_spy)
        return events_aapl, events_spy, hub

    events_aapl, events_spy, hub = asyncio.run(scenario())
    bars = [e.data for e in events_aapl if e.type == "bar"]
    trades = [e.data for e in events_aapl if e.type == "trade"]
    assert [(b["close"], b["revision"]) for b in bars] == [("101.00", 0), ("101.20", 1)]
    assert len(trades) == 1  # the latest trade per poll, with the arrival count
    assert trades[0]["price"] == "101.32"
    assert trades[0]["arrived"] == 3
    assert [e.type for e in events_spy] == ["quote"]  # SPY's client gets no AAPL data
    assert hub.clients == 0


def test_hub_delivers_a_bar_whose_transaction_committed_late(factory):
    aapl = _asset_id(factory, "AAPL")

    async def scenario() -> list[str]:
        hub = LiveUpdateHub(factory, feed="iex", clock=FixedClock(NOW), poll_interval=3600)
        sub = await hub.subscribe({aapl: "AAPL"})
        late = factory()
        late.execute(
            insert(MarketBarRecord),
            [_bar_row(aapl, NOW - timedelta(minutes=2), "100.00")],
        )  # holds the lower id, not committed yet
        mock_bar(factory, "AAPL", "1Min", NOW - timedelta(minutes=1), "101.00")
        await hub.poll_once()
        late.commit()
        late.close()
        await hub.poll_once()
        events = [sub.queue.get_nowait().data["close"] for _ in range(sub.queue.qsize())]
        await hub.close()
        return events

    assert asyncio.run(scenario()) == ["101.00", "100.00"]


def _bar_row(asset_id: uuid.UUID, at: datetime, close: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "provider": "alpaca",
        "feed": "iex",
        "provider_symbol": "AAPL",
        "timeframe": "1Min",
        "adjustment": PriceAdjustment.RAW,
        "event_time": at,
        "revision": 0,
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "volume": None,
        "vwap": None,
        "trade_count": None,
        "is_complete": True,
        "source": DataSource.LIVE_STREAM,
        "observation_kind": BarObservationKind.LIVE_INITIAL,
        "quality_status": DataQualityStatus.VALID,
        "received_at": at,
        "available_at": at,
        "original_available_at": None,
        "processed_at": at,
    }


# ------------------------------------------------------------------ SSE end to end
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    """The real application served by uvicorn on a free local port."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        # log_config=None: uvicorn must not replace the process's logging configuration
        # (other tests check Atlas's own logging setup).
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_config=None, timeout_graceful_shutdown=2
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


class _EventReader:
    """Reads Server-Sent Events in a thread, like a browser's EventSource."""

    def __init__(self, url: str) -> None:
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.url = url
        self.stop = threading.Event()
        self.content_type = ""
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self) -> None:
        try:
            with httpx.Client(timeout=30) as client, client.stream("GET", self.url) as response:
                self.content_type = response.headers["content-type"]
                event = None
                for line in response.iter_lines():
                    if self.stop.is_set():
                        return
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: ") and event:
                        self.events.put((event, json.loads(line[6:])))
        except httpx.HTTPError as exc:
            # The test server closing the stream at shutdown is expected; anything
            # earlier is surfaced by ``next`` (no event arrives).
            if not self.stop.is_set():
                self.events.put(("transport_error", {"error": repr(exc)}))

    def __enter__(self) -> _EventReader:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()

    def next(self, kind: str, timeout: float = 15) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no '{kind}' event within {timeout}s"
            event, data = self.events.get(timeout=remaining)
            assert event != "transport_error", data
            if event == kind:
                return data


@pytest.fixture
def served(factory, make_settings) -> Iterator[str]:
    app = create_app(
        make_settings(),
        clock=SystemClock(),
        health_checks={},
        market_data=_context(factory, datetime.now(UTC)),
    )
    with _Server(app) as url:
        yield url


def test_sse_delivers_stored_bars_to_a_connected_client(factory, served):
    with _EventReader(f"{served}{BASE}/live?symbol=AAPL") as reader:
        hello = reader.next("hello")
        assert reader.content_type.startswith("text/event-stream")
        assert hello["symbols"] == ["AAPL"]
        assert hello["feed"] == "iex"
        mock_bar(factory, "AAPL", "1Min", datetime(2026, 3, 10, 15, 1, tzinfo=UTC), "101.00")
        bar = reader.next("bar")
    assert bar["symbol"] == "AAPL"
    assert bar["close"] == "101.00"
    assert bar["event_time"] == "2026-03-10T15:01:00Z"


def test_live_event_flows_from_the_stream_worker_to_a_connected_client(factory, served):
    """FAKE provider stream -> real stream worker -> PostgreSQL -> API hub -> SSE client.

    The browser-side path is covered by the frontend tests; here the client is an
    EventSource-like HTTP reader. The worker is the only provider connection."""
    from app.tests.integration.test_stream_worker import _run_until, _worker

    minute = "2026-03-10T15:02:00Z"
    with _EventReader(f"{served}{BASE}/live?symbol=AAPL") as reader:
        reader.next("hello")
        frames = [frame(bar_msg("AAPL", minute, c=101.75))]

        async def scenario() -> None:
            async with FakeAlpacaStreamServer(Script(frames=[frames])) as server:
                worker = _worker(factory, server.url)
                await _run_until(worker, lambda: worker.counters["bars_stored"] >= 1)

        asyncio.run(scenario())
        bar = reader.next("bar")
    assert bar["symbol"] == "AAPL"
    assert bar["event_time"] == minute
    assert Decimal(bar["close"]) == Decimal("101.75")
    assert bar["source"] == "LIVE_STREAM"
    assert bar["observation_kind"] == "LIVE_INITIAL"

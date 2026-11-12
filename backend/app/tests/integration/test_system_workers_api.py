"""Phase 9 /api/v1/system/workers against a real PostgreSQL.

Every status row here is a MOCK operational record written by the test, not the state of a
real worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.clocks import FixedClock
from app.database.models.execution import PaperBrokerStateRecord
from app.database.models.market_data import RecoveryStatusRecord
from app.database.repositories import StreamRepository
from app.database.sessions import session_scope
from app.execution.enums import BrokerRunState
from app.main import create_app

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
URL = "/api/v1/system/workers"


@pytest.fixture
def factory(fresh_engine):
    return sessionmaker(bind=fresh_engine, expire_on_commit=False)


@pytest.fixture
def client(factory, make_settings):
    app = create_app(make_settings(), clock=FixedClock(NOW), health_checks={})
    app.state.session_factory = factory
    with TestClient(app) as c:
        yield c


def _by_worker(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {w["worker"]: w for w in body["workers"]}


def test_empty_database_reports_nothing_as_running(client):
    body = client.get(URL).json()
    workers = _by_worker(body)
    assert set(workers) == {
        "stream-worker",
        "recovery-worker",
        "news-worker",
        "discovery-worker",
        "strategy-worker",
        "ai-worker",
        "paper-execution-worker",
        "position-monitor-worker",
        "orchestrator-worker",
    }
    assert "RUNNING" not in body["summary"]
    for w in workers.values():
        assert w["health"] in {"NOT_REPORTED", "DISABLED"}
        assert w["last_heartbeat_at"] is None


def test_heartbeats_are_judged_by_age(client, factory):
    with session_scope(factory) as session:
        StreamRepository(session).upsert_status(
            "alpaca",
            "iex",
            {
                "worker_id": "mock",
                "enabled": True,
                "connection_state": "SUBSCRIBED",
                "auth_state": "AUTHENTICATED",
                "subscription_state": "ACTIVE",
                "requested_subscription": {},
                "confirmed_subscription": {},
                "heartbeat_at": NOW - timedelta(seconds=5),
                "last_error": "connect failed https://user:secret@example.invalid/x",
                "last_error_at": NOW - timedelta(hours=1),
                "metrics": {},
            },
        )
        session.add(
            RecoveryStatusRecord(
                provider="alpaca",
                feed="iex",
                worker_id="mock",
                enabled=True,
                state="IDLE",
                heartbeat_at=NOW - timedelta(hours=3),  # far beyond interval + grace
            )
        )
        session.add(
            PaperBrokerStateRecord(
                broker_id="atlas-paper-mock",
                status=BrokerRunState.RUNNING,
                fill_model="mock",
                last_heartbeat_at=NOW - timedelta(seconds=2),
            )
        )

    workers = _by_worker(client.get(URL).json())
    stream = workers["stream-worker"]
    assert stream["health"] == "RUNNING"
    assert stream["observation"] == "HEARTBEAT"
    assert stream["heartbeat_age_seconds"] == 5.0
    assert "secret" not in stream["last_error"]  # redacted before it leaves the API

    recovery = workers["recovery-worker"]
    assert recovery["health"] == "STALE"
    assert recovery["state"] == "IDLE"
    assert "probably not running" in recovery["detail"]

    broker = workers["paper-execution-worker"]
    assert broker["health"] == "RUNNING"
    assert broker["state"] == "RUNNING"

    # Workers that write no heartbeat are never reported RUNNING.
    for name in ("strategy-worker", "ai-worker", "position-monitor-worker"):
        assert workers[name]["health"] != "RUNNING"
        assert workers[name]["observation"] == "ACTIVITY"


def test_endpoint_is_read_only(client):
    assert client.post(URL).status_code == 405
    assert client.delete(URL).status_code == 405

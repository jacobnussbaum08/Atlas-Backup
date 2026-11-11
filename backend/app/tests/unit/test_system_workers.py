"""Worker health derivation and the /system/workers endpoint without a database (Phase 9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring.health import ComponentHealth, HealthStatus
from app.monitoring.workers import WorkerHealth, heartbeat_health

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
STALE = timedelta(seconds=60)


def _health(**kw: Any) -> tuple[WorkerHealth, str]:
    defaults: dict[str, Any] = {
        "now": NOW,
        "heartbeat_at": NOW,
        "stale_after": STALE,
        "enabled": True,
    }
    return heartbeat_health(**(defaults | kw))


def test_never_reported():
    assert _health(heartbeat_at=None, state=None)[0] is WorkerHealth.NOT_REPORTED


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SUBSCRIBED", WorkerHealth.RUNNING),
        ("RUNNING", WorkerHealth.RUNNING),
        ("IDLE", WorkerHealth.RUNNING),
        ("DEGRADED", WorkerHealth.DEGRADED),
        ("RECONNECTING", WorkerHealth.DEGRADED),
        ("PAUSED", WorkerHealth.DEGRADED),
        ("FAILED", WorkerHealth.FAILED),
        ("STOPPED", WorkerHealth.STOPPED),
        ("DISABLED", WorkerHealth.DISABLED),
    ],
)
def test_recorded_state_with_a_current_heartbeat(state, expected):
    assert _health(state=state, heartbeat_at=NOW - timedelta(seconds=5))[0] is expected


def test_old_heartbeat_overrides_a_running_state():
    health, detail = _health(state="SUBSCRIBED", heartbeat_at=NOW - timedelta(minutes=10))
    assert health is WorkerHealth.STALE
    assert "probably not running" in detail
    assert "SUBSCRIBED" in detail


def test_disabled_flag_wins_over_staleness():
    health, _ = _health(enabled=False, state="RUNNING", heartbeat_at=NOW - timedelta(days=2))
    assert health is WorkerHealth.DISABLED


def test_heartbeat_exactly_at_threshold_is_not_stale():
    assert _health(state="RUNNING", heartbeat_at=NOW - STALE)[0] is WorkerHealth.RUNNING


def test_no_threshold_never_marks_stale():
    health, _ = _health(state="RUNNING", stale_after=None, heartbeat_at=NOW - timedelta(days=9))
    assert health is WorkerHealth.RUNNING


def test_endpoint_without_a_database_is_unavailable(make_settings, fixed_clock):
    checks = {"database": lambda: ComponentHealth(name="database", status=HealthStatus.OK)}
    app = create_app(make_settings(), clock=fixed_clock, health_checks=checks)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/system/workers")
        assert response.status_code == 503
        assert client.post("/api/v1/system/workers").status_code == 405

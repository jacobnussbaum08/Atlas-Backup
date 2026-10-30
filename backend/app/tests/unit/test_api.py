"""API tests with dependency checks replaced by explicit stubs (no external services)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError
from app.main import create_app
from app.monitoring.health import ComponentHealth, HealthStatus
from app.tests.conftest import FIXED_NOW


def _ok(name: str):
    return lambda: ComponentHealth(name=name, status=HealthStatus.OK, latency_ms=1.0)


def _failing(name: str):
    return lambda: ComponentHealth(name=name, status=HealthStatus.ERROR, detail="down")


@pytest.fixture
def build_client(make_settings, fixed_clock):
    def factory(checks=None, **settings_overrides):
        checks = {"database": _ok("database"), "redis": _ok("redis")} if checks is None else checks
        app = create_app(
            make_settings(**settings_overrides), clock=fixed_clock, health_checks=checks
        )
        return TestClient(app, raise_server_exceptions=False)

    return factory


def test_health_reports_liveness_and_paper_mode(build_client, fixed_clock):
    with build_client() as client:
        fixed_clock.advance(timedelta(seconds=90))
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "PAPER"
    assert body["environment"] == "test"
    assert body["service"] == "atlas-ai-trader"
    assert body["uptime_seconds"] == 90.0
    assert body["time"] == (FIXED_NOW + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")


def test_health_does_not_call_dependency_checks(build_client):
    def explode():
        raise AssertionError("liveness must not touch dependencies")

    with build_client(checks={"database": explode}) as client:
        assert client.get("/health").status_code == 200


def test_ready_when_all_components_ok(build_client):
    with build_client() as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["trading_mode"] == "PAPER"
    assert [c["name"] for c in body["components"]] == ["database", "redis"]


def test_not_ready_when_any_component_fails(build_client):
    with build_client(checks={"database": _ok("database"), "redis": _failing("redis")}) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"][1] == {
        "name": "redis",
        "status": "error",
        "latency_ms": None,
        "detail": "down",
    }


def test_not_ready_without_any_checks(build_client):
    with build_client(checks={}) as client:
        assert client.get("/ready").status_code == 503


def test_request_id_is_echoed_or_generated(build_client):
    with build_client() as client:
        echoed = client.get("/health", headers={"X-Request-ID": "abc-123"})
        generated = client.get("/health")
        rejected = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})

    assert echoed.headers["X-Request-ID"] == "abc-123"
    assert len(generated.headers["X-Request-ID"]) == 32
    assert rejected.headers["X-Request-ID"] != "bad id\nwith newline"


def test_unhandled_errors_return_generic_body(build_client):
    with build_client() as client:

        @client.app.get("/boom")
        def boom():
            raise RuntimeError("internal detail postgresql://u:pw@h/d")

        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "pw" not in response.text
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_atlas_errors_map_to_their_status(build_client):
    with build_client() as client:

        @client.app.get("/missing")
        def missing():
            raise NotFoundError("asset not found")

        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_cors_is_restricted_to_configured_origins(build_client):
    with build_client(cors_origins=["http://localhost:5173"]) as client:
        allowed = client.get("/health", headers={"Origin": "http://localhost:5173"})
        denied = client.get("/health", headers={"Origin": "http://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_exposes_no_order_endpoints(build_client):
    with build_client() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/health",
        "/ready",
        "/api/v1/market-data/providers",
        "/api/v1/market-data/historical-fetch",
        "/api/v1/market-data/ingestion-jobs",
        "/api/v1/market-data/ingestion-jobs/{job_id}",
        "/api/v1/market-data/quality-events",
        "/api/v1/market-data/assets",
        "/api/v1/market-data/assets/{asset_id}/history",
        "/api/v1/market-data/candles",
        # 2C live streaming (read-only)
        "/api/v1/market-data/stream",
        "/api/v1/market-data/stream/sessions",
        "/api/v1/market-data/stream/gaps",
        "/api/v1/market-data/quotes",
        "/api/v1/market-data/trades",
        # 2D completeness and recovery
        "/api/v1/market-data/completeness",
        "/api/v1/market-data/gaps",
        "/api/v1/market-data/recovery-jobs",
        "/api/v1/market-data/recovery-requests",
        "/api/v1/market-data/pipeline-health",
        "/api/v1/market-data/metrics",
    }
    # Market trades (exchange prints) are read-only market data, not trading: the one
    # allowed path containing "trade" must expose GET only.
    market_data_reads = {"/api/v1/market-data/trades"}
    forbidden = ("order", "trade", "broker", "position", "account", "execution")
    assert not [
        p
        for p in paths
        if p not in market_data_reads and any(word in p.lower() for word in forbidden)
    ]
    for path in market_data_reads:
        assert set(paths[path]) == {"get"}
    writes = {p for p, methods in paths.items() if set(methods) - {"get"}}
    # Operator-gated market-data actions only: a historical fetch, and a request to verify
    # a range (it records gaps; the recovery worker retrieves market data).
    assert writes == {
        "/api/v1/market-data/historical-fetch",
        "/api/v1/market-data/recovery-requests",
    }

"""Every write route is an operator action (spec 24: protect administrative endpoints).

A guard over the whole application, read from its OpenAPI document so that it follows the
routes the server really serves: any operation that is not a read must take the operator
token and must refuse a request without one before its handler runs. A new endpoint that
forgets its dependency fails here.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring.health import ComponentHealth, HealthStatus

READ_METHODS = {"get", "head", "options"}
OPERATOR_HEADER = "X-Atlas-Operator-Token"
TRADING_CONTROL_PATHS = {
    "/api/v1/orchestration/controls/pause",
    "/api/v1/orchestration/controls/disable",
    "/api/v1/orchestration/controls/emergency-stop",
    "/api/v1/risk/kill-switch/engage",
}
# Path segments no route may have: enabling trading, resuming it, clearing an emergency
# stop and releasing the kill switch are CLI-only operator actions, and there is no broker.
FORBIDDEN_SEGMENTS = {"enable", "resume", "clear", "clear-emergency", "release", "broker"}


@pytest.fixture
def build_client(make_settings, fixed_clock):
    def factory(**settings_overrides: Any) -> TestClient:
        checks = {
            name: (lambda name=name: ComponentHealth(name=name, status=HealthStatus.OK))
            for name in ("database", "redis")
        }
        app = create_app(
            make_settings(**settings_overrides), clock=fixed_clock, health_checks=checks
        )
        return TestClient(app, raise_server_exceptions=False)

    return factory


def _write_operations(client: TestClient) -> list[tuple[str, str, dict[str, Any]]]:
    paths = client.get("/openapi.json").json()["paths"]
    return sorted(
        (method, path, operation)
        for path, operations in paths.items()
        for method, operation in operations.items()
        if method not in READ_METHODS
    )


def test_every_write_operation_takes_the_operator_token(build_client) -> None:
    with build_client() as client:
        operations = _write_operations(client)
    assert len(operations) >= 30  # the guard is looking at the real application
    assert {path for _, path, _ in operations} >= TRADING_CONTROL_PATHS
    unprotected = [
        f"{method.upper()} {path}"
        for method, path, operation in operations
        if OPERATOR_HEADER
        not in {p["name"] for p in operation.get("parameters", []) if p["in"] == "header"}
    ]
    assert unprotected == []


def test_write_operations_are_refused_without_a_token_outside_development(build_client) -> None:
    """In the paper environment an unauthenticated write never reaches its handler,
    whatever its path values and body: 401/403, or 503 ``dependency_unavailable`` where the
    route's service is resolved first and is not configured in this database-less
    application (those routes' token checks are exercised by their integration tests)."""
    with build_client(environment="paper", postgres_password="unit-test-only-secret") as client:
        operations = _write_operations(client)
        assert len(operations) >= 30
        authenticated = 0
        for method, path, _ in operations:
            url = path.replace("{", "").replace("}", "")
            for headers in ({}, {OPERATOR_HEADER: "not-a-real-token"}):
                response = client.request(method.upper(), url, json={}, headers=headers)
                refused = response.status_code in (401, 403) or (
                    response.status_code == 503
                    and response.json()["error"]["code"] == "dependency_unavailable"
                )
                assert refused, (method, path, response.status_code, response.text[:200])
                if response.status_code in (401, 403):
                    authenticated += 1
        assert authenticated >= len(operations)  # most are refused by the token check itself


def test_no_route_can_enable_resume_or_release_trading(build_client) -> None:
    with build_client() as client:
        paths = set(client.get("/openapi.json").json()["paths"])
    assert len(paths) > 100
    offenders = sorted(
        path for path in paths if FORBIDDEN_SEGMENTS & set(path.strip("/").split("/"))
    )
    assert offenders == []

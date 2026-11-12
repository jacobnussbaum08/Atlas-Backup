"""API tests with dependency checks replaced by explicit stubs (no external services)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError
from app.main import create_app
from app.monitoring.health import ComponentHealth, HealthStatus
from app.tests.conftest import FIXED_NOW

BACKTEST_REPRODUCE = "/api/v1/backtests/{run_id}/reproduce"
ORCHESTRATION_STOPS = (
    "/api/v1/orchestration/controls/pause",
    "/api/v1/orchestration/controls/disable",
    "/api/v1/orchestration/controls/emergency-stop",
)
ALERT_ACK = "/api/v1/orchestration/alerts/{alert_id}/acknowledge"
VALIDATION_READS = (
    "/api/v1/validation/overview",
    "/api/v1/validation/options",
    "/api/v1/validation/compare",
    "/api/v1/validation/runs/{run_id}",
    "/api/v1/validation/runs/{run_id}/folds",
    "/api/v1/validation/runs/{run_id}/trials",
    "/api/v1/validation/runs/{run_id}/robustness",
    "/api/v1/validation/runs/{run_id}/sensitivity",
    "/api/v1/validation/runs/{run_id}/segments",
    "/api/v1/validation/models/compare",
    "/api/v1/validation/comparisons",
    "/api/v1/validation/champions",
    "/api/v1/validation/candidates/{candidate_id}",
    "/api/v1/validation/promotions",
    "/api/v1/validation/monitoring",
    "/api/v1/validation/alerts",
)
# Paths with both a list (GET) and a create (POST).
VALIDATION_LISTS = ("/api/v1/validation/runs", "/api/v1/validation/candidates")
VALIDATION_WRITES = (
    *VALIDATION_LISTS,
    "/api/v1/validation/runs/{run_id}/reproduce",
    "/api/v1/validation/runs/{run_id}/holdout",
    "/api/v1/validation/models/comparisons",
    "/api/v1/validation/champions/adopt",
    "/api/v1/validation/candidates/{candidate_id}/transition",
    "/api/v1/validation/candidates/{candidate_id}/shadow-evaluation",
    "/api/v1/validation/monitoring/run",
    "/api/v1/validation/monitoring/rules",
)


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
        # 2E dashboard read models and live distribution (read-only)
        "/api/v1/market-data/market-status",
        "/api/v1/market-data/snapshots",
        "/api/v1/market-data/configuration",
        "/api/v1/market-data/audit-events",
        "/api/v1/market-data/live",
        # 3A asset universe (read-only) and discovery operations
        "/api/v1/assets",
        "/api/v1/assets/discovery-pool",
        "/api/v1/assets/qualified",
        "/api/v1/assets/watchlist",
        "/api/v1/assets/{asset_id}",
        "/api/v1/assets/{asset_id}/qualification",
        "/api/v1/assets/{asset_id}/history",
        "/api/v1/discovery/jobs",
        "/api/v1/discovery/jobs/{job_id}",
        "/api/v1/discovery/rankings",
        "/api/v1/discovery/summary",
        "/api/v1/discovery/config",
        "/api/v1/discovery/config/rule-set",
        "/api/v1/discovery/config/scoring",
        "/api/v1/discovery/run",
        "/api/v1/discovery/refresh",
        "/api/v1/market-sessions",
        # 3B market intelligence (descriptive; runs are operator-gated)
        "/api/v1/intelligence/market",
        "/api/v1/intelligence/regimes/history",
        "/api/v1/intelligence/assets",
        "/api/v1/intelligence/assets/{asset_id}",
        "/api/v1/intelligence/correlations",
        "/api/v1/intelligence/runs",
        "/api/v1/intelligence/runs/{run_id}",
        "/api/v1/intelligence/config",
        # 3C news and events (read-only; historical retrieval is operator-gated)
        "/api/v1/news/articles",
        "/api/v1/news/articles/{article_id}",
        "/api/v1/news/assets/{asset_id}",
        "/api/v1/news/assets/{asset_id}/events",
        "/api/v1/news/events",
        "/api/v1/news/taxonomy",
        "/api/v1/news/summary",
        "/api/v1/news/sources",
        "/api/v1/news/ingestion-jobs",
        "/api/v1/news/ingestion-jobs/{job_id}",
        "/api/v1/news/providers",
        "/api/v1/news/stream",
        "/api/v1/news/stream/sessions",
        "/api/v1/news/economic-calendar",
        # 4A strategy laboratory (proposals are read-only; parameter versions and
        # enable/disable are operator-gated)
        "/api/v1/strategies",
        "/api/v1/strategies/features",
        "/api/v1/strategies/evaluations",
        "/api/v1/strategies/evaluations/{evaluation_id}",
        "/api/v1/strategies/feature-snapshots/{snapshot_id}",
        "/api/v1/strategies/proposals",
        "/api/v1/strategies/proposals/{proposal_id}",
        "/api/v1/strategies/conflicts",  # 4B: recorded proposal conflicts, read-only
        "/api/v1/strategies/{strategy_id}",
        "/api/v1/strategies/{strategy_id}/versions",
        "/api/v1/strategies/{strategy_id}/configurations",
        "/api/v1/strategies/{strategy_id}/status",
        # 5A machine learning (read-only, plus an operator-gated lifecycle transition;
        # predictions are analytical records, nothing is submitted)
        "/api/v1/ml/overview",
        "/api/v1/ml/datasets",
        "/api/v1/ml/datasets/{version_id}",
        "/api/v1/ml/runs",
        "/api/v1/ml/runs/{run_id}",
        "/api/v1/ml/compare",
        "/api/v1/ml/models",
        "/api/v1/ml/models/{model_id}",
        "/api/v1/ml/models/{model_id}/status",
        "/api/v1/ml/predictions",
        "/api/v1/ml/predictions/{prediction_id}",
        "/api/v1/ml/predictions/{prediction_id}/replay",
        # Phase 5B: read-only decision and LLM records (no endpoint runs, approves or sends)
        "/api/v1/ai/overview",
        "/api/v1/ai/policies",
        "/api/v1/ai/runs",
        "/api/v1/ai/runs/{run_id}",
        "/api/v1/ai/runs/{run_id}/replay",
        "/api/v1/ai/rankings",
        "/api/v1/ai/decisions",
        "/api/v1/ai/decisions/{decision_id}",
        "/api/v1/ai/decisions/{decision_id}/evidence",
        "/api/v1/ai/decisions/{decision_id}/models",
        "/api/v1/ai/conflicts",
        "/api/v1/ai/llm/analyses",
        "/api/v1/ai/llm/analyses/{analysis_id}",
        "/api/v1/ai/llm/requests",
        "/api/v1/ai/llm/usage",
        "/api/v1/ai/evaluations",
        # Phase 6A portfolio and accounting: GET only. Postings, funding, checkpoints and
        # reconciliation are CLI jobs; nothing creates, sizes, approves or submits an order.
        "/api/v1/portfolio/accounts",
        "/api/v1/portfolio/overview",
        "/api/v1/portfolio/cash",
        "/api/v1/portfolio/holdings",
        "/api/v1/portfolio/pnl",
        "/api/v1/portfolio/snapshots",
        "/api/v1/accounting/chart",
        "/api/v1/accounting/templates",
        "/api/v1/accounting/journal",
        "/api/v1/accounting/journal/{entry_id}",
        "/api/v1/accounting/balances",
        "/api/v1/accounting/reconciliation",
        "/api/v1/accounting/reconciliation/{run_id}",
        "/api/v1/accounting/fx-rates",
        # Phase 6B risk engine: reads, plus the fail-safe kill-switch engage (operator token).
        # Limits, resets, releases and assessments are operator CLI actions.
        "/api/v1/risk/state",
        "/api/v1/risk/limits",
        "/api/v1/risk/exposure",
        "/api/v1/risk/exposure/snapshots",
        "/api/v1/risk/assessments",
        "/api/v1/risk/assessments/{assessment_id}",
        "/api/v1/risk/circuit-breakers",
        "/api/v1/risk/circuit-breakers/events",
        "/api/v1/risk/configurations",
        "/api/v1/risk/events",
        "/api/v1/risk/kill-switch/engage",
        # Phase 6C reservations: GET only. Reserving, consuming and releasing are service
        # interfaces (the execution phase) and operator CLI actions, never API writes.
        "/api/v1/risk/reservations",
        "/api/v1/risk/reservations/capacity",
        "/api/v1/risk/reservations/events",
        "/api/v1/risk/reservations/reconciliations",
        "/api/v1/risk/reservations/{reservation_id}",
        # Phase 7A simulated paper orders and the paper broker: GET only. Submission is the
        # execution coordinator's service interface; cancellation and controlled test
        # events are operator CLI actions. No live brokerage control exists.
        "/api/v1/paper-orders",
        "/api/v1/paper-orders/open",
        "/api/v1/paper-orders/summary",
        "/api/v1/paper-orders/events",
        "/api/v1/paper-orders/requests",
        "/api/v1/paper-orders/{order_id}",
        "/api/v1/paper-orders/{order_id}/events",
        "/api/v1/paper-broker/status",
        # Phase 7B simulated positions and execution analytics: GET only.
        "/api/v1/paper-positions",
        "/api/v1/paper-positions/summary",
        "/api/v1/paper-positions/{position_id}",
        "/api/v1/paper-execution/analytics",
        "/api/v1/paper-execution/models",
        # Phase 8A backtesting: replays of stored history. Two operator-gated writes (create
        # a run, reproduce a completed one); everything else is GET.
        "/api/v1/backtests",
        "/api/v1/backtests/options",
        "/api/v1/backtests/compare",
        "/api/v1/backtests/{run_id}",
        BACKTEST_REPRODUCE,
        "/api/v1/backtests/{run_id}/progress",
        "/api/v1/backtests/{run_id}/equity",
        "/api/v1/backtests/{run_id}/metrics",
        "/api/v1/backtests/{run_id}/benchmarks",
        "/api/v1/backtests/{run_id}/trades",
        "/api/v1/backtests/{run_id}/trades/{seq}",
        "/api/v1/backtests/{run_id}/orders",
        "/api/v1/backtests/{run_id}/fills",
        "/api/v1/backtests/{run_id}/proposals",
        "/api/v1/backtests/{run_id}/rejections",
        "/api/v1/backtests/{run_id}/events",
        "/api/v1/backtests/{run_id}/errors",
        "/api/v1/backtests/{run_id}/evaluations",
        "/api/v1/backtests/{run_id}/journal",
        # Phase 8B validation laboratory: replays of stored history and governance records.
        # Operator-gated writes create, reproduce or holdout-evaluate a run, store a model
        # comparison, record lifecycle decisions and run the monitor; nothing promotes itself.
        *VALIDATION_READS,
        *VALIDATION_WRITES,
        # Phase 9 dashboard: what the background workers recorded (GET only; it controls
        # nothing).
        "/api/v1/system/workers",
        # Phase 10A orchestration: reads, and fail-safe controls that can only stop trading.
        "/api/v1/orchestration/status",
        "/api/v1/orchestration/controls/history",
        "/api/v1/orchestration/intents",
        "/api/v1/orchestration/intents/{intent_id}",
        "/api/v1/orchestration/jobs",
        *ORCHESTRATION_STOPS,
        # Phase 10B: protection gate, alerts (acknowledging one is append-only), metrics.
        "/api/v1/orchestration/protection",
        "/api/v1/orchestration/alerts",
        ALERT_ACK,
        "/api/v1/orchestration/metrics",
    }
    # Market trades (exchange prints) are read-only market data, not trading: the one
    # allowed path containing "trade" must expose GET only.
    market_data_reads = {"/api/v1/market-data/trades"}
    # Phase 6A: the double-entry ledger ("accounting") and the list of simulated paper
    # accounts are read-only records, not brokerage accounts: each must expose GET only.
    accounting_reads = {p for p in paths if p.startswith("/api/v1/accounting/")} | {
        "/api/v1/portfolio/accounts"
    }
    # Phase 7A: simulated paper orders and the simulated paper broker, read-only.
    paper_reads = {
        p
        for p in paths
        if p.startswith(
            (
                "/api/v1/paper-orders",
                "/api/v1/paper-broker/",
                "/api/v1/paper-positions",
                "/api/v1/paper-execution/",
            )
        )
    }
    assert len(paper_reads) == 13
    # Phase 8A: a backtest's simulated orders, fills and trades are records of a replay of
    # stored history, not orders of the paper account: GET only (D-181).
    backtest_reads = {
        p for p in paths if p.startswith("/api/v1/backtests/") and p != BACKTEST_REPRODUCE
    }
    allowed_reads = market_data_reads | accounting_reads | paper_reads | backtest_reads
    forbidden = ("order", "trade", "broker", "position", "account", "execution")
    assert not [
        p for p in paths if p not in allowed_reads and any(word in p.lower() for word in forbidden)
    ]
    for path in allowed_reads:
        assert set(paths[path]) == {"get"}
    assert not [p for p in paths if p.startswith("/api/v1/portfolio/") and set(paths[p]) != {"get"}]
    writes = {p for p, methods in paths.items() if set(methods) - {"get"}}
    # Operator-gated actions only: a historical fetch, a request to verify a range (it
    # records gaps; the recovery worker retrieves market data), starting discovery or a
    # reference-data refresh, new versions of the qualification/scoring configuration,
    # starting a market intelligence run, a historical news retrieval, and a new strategy
    # parameter version or enable/disable (no endpoint approves or submits a proposal), and
    # an explicit model lifecycle transition (Phase 5A; nothing is promoted automatically).
    assert writes == {
        "/api/v1/market-data/historical-fetch",
        "/api/v1/market-data/recovery-requests",
        "/api/v1/discovery/run",
        "/api/v1/discovery/refresh",
        "/api/v1/discovery/config/rule-set",
        "/api/v1/discovery/config/scoring",
        "/api/v1/intelligence/runs",
        "/api/v1/news/ingestion-jobs",
        "/api/v1/strategies/{strategy_id}/versions",
        "/api/v1/strategies/{strategy_id}/status",
        "/api/v1/ml/models/{model_id}/status",
        "/api/v1/risk/kill-switch/engage",
        # Phase 8A: create a backtest run, re-execute a completed one (replays only).
        "/api/v1/backtests",
        BACKTEST_REPRODUCE,
        # Phase 8B: validation runs and explicit champion/challenger decisions (D-185..).
        *VALIDATION_WRITES,
        # Phase 10A: pause, disable, emergency stop (D-206). Nothing enables trading.
        *ORCHESTRATION_STOPS,
        # Phase 10B: acknowledging an alert (an append-only note; it changes nothing else).
        ALERT_ACK,
    }
    for path in (*ORCHESTRATION_STOPS, ALERT_ACK):
        assert set(paths[path]) == {"post"}
    assert not [
        p
        for p in paths
        if p.startswith("/api/v1/orchestration/")
        and any(word in p for word in ("enable", "resume", "clear"))
    ]
    assert set(paths["/api/v1/backtests"]) == {"get", "post"}
    for path in VALIDATION_WRITES:
        expected = {"get", "post"} if path in VALIDATION_LISTS else {"post"}
        assert set(paths[path]) == expected, path
    assert set(paths[BACKTEST_REPRODUCE]) == {"post"}
    # Phase 6B: the only risk write can only halt; it cannot release, reset or loosen.
    assert set(paths["/api/v1/risk/kill-switch/engage"]) == {"post"}
    assert not [p for p in paths if p.startswith("/api/v1/risk/") and "release" in p]
    # Phase 6C: reservations are read-only through the API.
    assert not [
        p for p in paths if p.startswith("/api/v1/risk/reservations") and set(paths[p]) != {"get"}
    ]
    assert not [
        p
        for p in paths
        if p.startswith("/api/v1/risk/")
        and p != "/api/v1/risk/kill-switch/engage"
        and set(paths[p]) != {"get"}
    ]

"""Phase 10A orchestration API, CLI and worker loop against a real PostgreSQL.

TEST FIXTURE data only (see ``orchestration_support``); every order is a simulated paper order.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.exceptions import AtlasError
from app.database.models.orchestration import OrchestratorStatusRecord
from app.database.sessions import session_scope
from app.main import create_app
from app.orchestration import cli
from app.orchestration.enums import ControlAction, IntentStatus, TradingMode
from app.orchestration.orchestrator import STATUS_NAME
from app.tests.integration.orchestration_support import (
    OPERATOR,
    Bench,
    make_bench,
    settings,
)
from app.workers import trading_orchestrator

pytestmark = pytest.mark.integration


@pytest.fixture
def bench(fresh_engine) -> Bench:
    # The API and CLI default to the PRIMARY paper account.
    b = make_bench(fresh_engine, name="orch-api-test")
    primary = b.ex.risk.books.account("Atlas paper account", test=False)
    b.ex.risk.configure(primary)
    b.ex.risk.reconcile(primary)
    b.account, b.account_name = primary, "Atlas paper account"
    return b


@pytest.fixture
def api(bench, make_settings):
    app = create_app(
        make_settings(), clock=bench.clock, health_checks={}, orchestration=bench.control
    )
    app.state.session_factory = bench.factory
    with TestClient(app) as client:
        yield client


def _status(bench: Bench) -> OrchestratorStatusRecord:
    with session_scope(bench.factory) as session:
        row = session.get(OrchestratorStatusRecord, STATUS_NAME)
        assert row is not None
        session.expunge(row)
        return row


# ====================================================================== API
def test_status_reports_disabled_by_default(api):
    body = api.get("/api/v1/orchestration/status").json()
    assert body["simulated"] is True
    assert body["control"]["mode"] == "DISABLED"
    assert body["control"]["default"] is True
    assert body["control"]["entries_allowed"] is False
    assert body["orchestrator"]["health"] in {"NOT_REPORTED", "DISABLED"}
    assert body["intents"]["ORDER_CREATED"] == 0
    assert api.get("/api/v1/orchestration/controls/history").json() == []


def test_api_offers_fail_safe_controls_only(api, bench):
    # There is no way to enable, resume or clear an emergency through the API.
    for path in ("enable", "resume", "clear-emergency"):
        response = api.post(f"/api/v1/orchestration/controls/{path}", json={"reason": "x" * 5})
        assert response.status_code in (404, 405)
    refused = api.post("/api/v1/orchestration/controls/pause", json={"reason": "pause it"})
    assert refused.status_code == 409  # DISABLED cannot be paused
    bench.enable()
    paused = api.post("/api/v1/orchestration/controls/pause", json={"reason": "dashboard pause"})
    assert paused.status_code == 200
    assert paused.json()["control"]["mode"] == "PAUSED"
    assert paused.json()["control"]["actor"] == "operator:api"
    disabled = api.post("/api/v1/orchestration/controls/disable", json={"reason": "stop it"})
    assert disabled.json()["control"]["mode"] == "DISABLED"
    stop = api.post(
        "/api/v1/orchestration/controls/emergency-stop", json={"reason": "dashboard e-stop"}
    )
    assert stop.status_code == 200
    body = stop.json()
    assert body["control"]["mode"] == "EMERGENCY_STOP"
    assert body["kill_switch_engaged_now"] is True
    assert "clear-emergency" in body["clear"]
    assert api.post("/api/v1/orchestration/controls/pause", json={"reason": ""}).status_code == 422
    history = api.get("/api/v1/orchestration/controls/history").json()
    assert [h["action"] for h in history] == ["EMERGENCY_STOP", "DISABLE", "PAUSE", "ENABLE"]


def test_controls_require_the_operator_token_when_one_is_configured(bench, make_settings):
    app = create_app(
        make_settings(operator_token="tok-test-orchestration"),
        clock=bench.clock,
        health_checks={},
        orchestration=bench.control,
    )
    app.state.session_factory = bench.factory
    with TestClient(app) as client:
        url = "/api/v1/orchestration/controls/emergency-stop"
        assert client.post(url, json={"reason": "no token"}).status_code == 401
        wrong = {"X-Atlas-Operator-Token": "wrong"}
        assert client.post(url, json={"reason": "bad token"}, headers=wrong).status_code == 401
        assert bench.mode() is TradingMode.DISABLED
        good = {"X-Atlas-Operator-Token": "tok-test-orchestration"}
        response = client.post(url, json={"reason": "good token"}, headers=good)
        assert response.status_code == 200
        assert "tok-test-orchestration" not in response.text
        assert client.get("/api/v1/orchestration/status").status_code == 200  # reads are open


def test_intents_and_jobs_are_listed(api, bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    asyncio.run(bench.orchestrator().orchestrator.tick())
    [intent] = api.get("/api/v1/orchestration/intents").json()
    assert intent["status"] == "ORDER_CREATED"
    assert intent["order_id"] is not None
    one = api.get(f"/api/v1/orchestration/intents/{intent['id']}").json()
    assert one["reservation_id"] == intent["reservation_id"]
    assert api.get("/api/v1/orchestration/intents", params={"status": "DEAD"}).json() == []
    jobs = api.get("/api/v1/orchestration/jobs").json()
    assert {j["kind"] for j in jobs} >= {"RISK_MONITOR", "ACCOUNTING_RECONCILE"}
    status = api.get("/api/v1/orchestration/status").json()
    assert status["intents"]["ORDER_CREATED"] == 1
    assert status["orchestrator"]["state"] == "RUNNING"
    workers = {w["worker"]: w for w in api.get("/api/v1/system/workers").json()["workers"]}
    assert workers["orchestrator-worker"]["health"] == "RUNNING"
    assert workers["orchestrator-worker"]["observation"] == "HEARTBEAT"


# ====================================================================== CLI
def test_cli_controls_and_reads(bench, capsys):
    configured = settings(account_name=bench.account_name)

    def run(*argv: str) -> tuple[int, Any]:
        try:
            code = cli.run(bench.control, configured, cli._parser().parse_args(list(argv)))
        except (AtlasError, ValueError):
            code = 1  # what main() returns for a refusal
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    code, out = run("status")
    assert code == 0
    assert out["control"]["mode"] == "DISABLED"
    code, out = run("enable", "--actor", OPERATOR, "--reason", "start paper trading")
    assert code == 0
    assert out["mode"] == "ENABLED"
    code, _ = run("enable", "--actor", "system:orchestrator", "--reason", "self-enable")
    assert code == 1  # only an operator
    code, out = run("emergency-stop", "--actor", OPERATOR, "--reason", "drill")
    assert code == 0
    assert out["state"]["mode"] == "EMERGENCY_STOP"
    code, _ = run("enable", "--actor", OPERATOR, "--reason", "straight back on")
    assert code == 1  # an emergency is cleared to DISABLED first
    code, out = run("clear-emergency", "--actor", OPERATOR, "--reason", "drill over")
    assert out["mode"] == "DISABLED"
    code, out = run("history")
    assert [h["action"] for h in out] == ["CLEAR_EMERGENCY", "EMERGENCY_STOP", "ENABLE"]
    code, out = run("intents")
    assert out == []
    code, out = run("jobs")
    assert out == []


# ====================================================================== worker
def test_worker_loop_records_status_and_stops_cleanly(bench):
    runtime = bench.orchestrator()
    stop = asyncio.Event()
    code = asyncio.run(
        trading_orchestrator.run(runtime.orchestrator, settings(), stop, max_ticks=2)
    )
    assert code == 0
    row = _status(bench)
    assert row.state.value == "STOPPED"
    assert row.stopped_at is not None
    assert row.started_at is not None
    assert row.counters["ticks"] == 2
    assert row.worker_id == "orchestrator:test-1"


def test_worker_survives_a_database_outage_and_recovers(bench, monkeypatch):
    """The tick fails while 'the database is down' (simulated: the control read raises);
    the worker keeps running, records DEGRADED once it can, and the next tick recovers
    without duplicating anything."""
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator()
    from app.orchestration import controls as module

    real = module.current
    outage = {"left": 2}

    def flaky(session: Any, account_id: Any) -> Any:
        if outage["left"] > 0:
            outage["left"] -= 1
            raise OperationalError("SELECT 1", {}, ConnectionError("server closed (FIXTURE)"))
        return real(session, account_id)

    monkeypatch.setattr(module, "current", flaky)
    stop = asyncio.Event()
    fast = settings(tick_seconds=1)
    asyncio.run(trading_orchestrator.run(runtime.orchestrator, fast, stop, max_ticks=3))
    row = _status(bench)
    assert row.counters.get("tick_failures") == 2
    assert row.state.value == "STOPPED"
    assert "OperationalError" in (row.last_error or "")
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert len(bench.orders()) == 1


def test_worker_restart_resumes_pending_work_without_duplicates(bench, monkeypatch):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    first = bench.orchestrator(worker_id="orchestrator:before-crash")
    # Crash between intake and delivery: the delivery step never runs.
    monkeypatch.setattr(first.orchestrator.dispatcher, "dispatch", lambda **_k: None)
    asyncio.run(first.orchestrator.tick())
    [intent] = bench.intents()
    assert intent.status is IntentStatus.RECEIVED
    monkeypatch.undo()
    bench.clock.advance(timedelta(seconds=30))
    second = bench.orchestrator(worker_id="orchestrator:after-restart")
    asyncio.run(
        trading_orchestrator.run(second.orchestrator, settings(), asyncio.Event(), max_ticks=2)
    )
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert len(bench.orders()) == 1
    assert len(bench.reservations()) == 1
    assert _status(bench).worker_id == "orchestrator:after-restart"


def test_disabled_worker_exits_without_touching_the_database(monkeypatch, make_settings):
    monkeypatch.setenv("ORCHESTRATOR_WORKER_ENABLED", "false")
    from app.orchestration.config import get_orchestrator_settings

    get_orchestrator_settings.cache_clear()
    monkeypatch.setattr(trading_orchestrator, "get_settings", lambda: make_settings())
    monkeypatch.setattr(
        trading_orchestrator,
        "build_engine",
        lambda _s: (_ for _ in ()).throw(AssertionError("no engine when disabled")),
    )
    try:
        assert trading_orchestrator.main([]) == 0
    finally:
        get_orchestrator_settings.cache_clear()


def test_pause_mid_operation_resolves_pending_intents_without_orders(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator()
    runtime.orchestrator.dispatcher.dispatch = lambda **_k: None
    asyncio.run(runtime.orchestrator.tick())
    bench.set_mode(ControlAction.PAUSE)
    asyncio.run(bench.orchestrator(worker_id="orchestrator:other").orchestrator.tick())
    [intent] = bench.intents()
    assert intent.status is IntentStatus.NOT_TRADED
    assert bench.orders() == []

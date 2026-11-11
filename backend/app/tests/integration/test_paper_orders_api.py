"""Phase 7A Paper Orders API (GET only) and CLI against a real PostgreSQL (TEST FIXTURE).

The primary paper account is used for the API's defaults; fills (controlled test events)
are only possible on CONTROLLED_TEST accounts, so those are read with ``account_id``.
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.execution import cli
from app.execution.monitor import PositionMonitor
from app.execution.reconciliation import ExecutionReconciler
from app.execution.runtime import ExecutionContext
from app.execution.simulation import ExecutionSimulator
from app.main import create_app
from app.tests.integration.execution_support import Execution, make_execution

pytestmark = pytest.mark.integration


@pytest.fixture
def ex(fresh_engine) -> Execution:
    return make_execution(fresh_engine)


@pytest.fixture
def ctx(ex) -> ExecutionContext:
    return ExecutionContext(
        session_factory=ex.factory,
        clock=ex.risk.clock,
        broker=ex.broker,
        service=ex.service,
        reconciler=ExecutionReconciler(ex.factory, ex.risk.clock),
        simulator=ExecutionSimulator(ex.service, ex.risk.clock),
        monitor=PositionMonitor(ex.service, ex.risk.clock, accounting=ex.rv.service._accounting),
    )


@pytest.fixture
def api(ex, ctx, make_settings):
    app = create_app(make_settings(), clock=ex.risk.clock, health_checks={}, execution=ctx)
    with TestClient(app) as client:
        yield client


def seed(ex: Execution):
    account = ex.account("api-test")
    a, b, c = ex.asset("APA"), ex.asset("APB"), ex.asset("APC")
    filled_r = ex.reservation(account, a, "api-res-0001")
    open_r = ex.reservation(account, b, "api-res-0002")
    cancel_r = ex.reservation(account, c, "api-res-0003")
    filled = ex.open_order(account, filled_r, "40", "api-order-0001")
    ex.fill(filled, "40", "99.60", "api-fill-0001")
    working = ex.open_order(account, open_r, "20", "api-order-0002")
    cancelled = ex.open_order(account, cancel_r, "10", "api-order-0003")
    ex.cancel(cancelled, "api-cancel-0003")
    ex.dispatch()
    ex.submit(ex.entry(account, open_r, "5"), "api-order-0004", by="strategy:momentum")
    return account, filled, working, cancelled, filled_r


def test_paper_order_reads(api, ex):
    account, filled, working, cancelled, filled_r = seed(ex)
    q = {"account_id": str(account)}
    orders = api.get("/api/v1/paper-orders", params=q).json()
    assert {o["id"] for o in orders} == {str(filled), str(working), str(cancelled)}
    assert all(o["simulated"] is True for o in orders)
    assert [o["id"] for o in api.get("/api/v1/paper-orders/open", params=q).json()] == [
        str(working)
    ]
    cancelled_rows = api.get("/api/v1/paper-orders", params=q | {"group": "cancelled"}).json()
    assert [o["id"] for o in cancelled_rows] == [str(cancelled)]
    assert api.get("/api/v1/paper-orders", params=q | {"status": "FILLED"}).json()[0]["id"] == str(
        filled
    )
    assert api.get("/api/v1/paper-orders", params=q | {"group": "rejected"}).json() == []

    detail = api.get(f"/api/v1/paper-orders/{filled}").json()
    assert "Simulated" in detail["notice"]
    assert detail["order"]["status"] == "FILLED"
    assert D(detail["order"]["average_fill_price"]) == D("99.6")
    assert [e["event_type"] for e in detail["events"]] == [
        "CREATED",
        "APPROVED",
        "ACCEPTED",
        "FILL",
    ]
    assert detail["fills"][0]["source"] == "CONTROLLED_TEST"
    link = detail["reservation"]
    assert link["reservation_id"] == str(filled_r)
    assert link["role"] == "CONSUMED_BY_FILLS"
    assert (D(link["consumed_quantity"]), D(link["released_quantity"])) == (D(40), D(52))
    assert detail["paper_broker"]["simulated"] is True
    assert detail["paper_broker"]["book"]["status"] == "FILLED"
    assert all(e["applied"] for e in detail["paper_broker"]["events"])
    assert {o["topic"] for o in detail["outbox"]} >= {"order.submit", "paper_broker.event"}
    events = api.get(f"/api/v1/paper-orders/{filled}/events").json()
    assert events == detail["events"]

    feed = api.get("/api/v1/paper-orders/events", params=q | {"event_type": "CANCEL_CONFIRMED"})
    assert [e["order_id"] for e in feed.json()] == [str(cancelled)]
    summary = api.get("/api/v1/paper-orders/summary", params=q).json()
    assert summary["counts"]["FILLED"] == 1
    assert summary["counts"]["CANCELLED"] == 1
    assert summary["open"] == 1
    assert summary["refused_requests"] == 1
    refused = api.get("/api/v1/paper-orders/requests", params=q | {"decision": "REFUSED"}).json()
    assert refused[0]["requested_by"] == "strategy:momentum"
    assert refused[0]["reasons"][0]["code"] == "UNAUTHORIZED_SUBMITTER"

    broker = api.get("/api/v1/paper-broker/status").json()
    assert broker["simulated"] is True
    assert broker["status"] == "RUNNING"
    assert broker["working_orders"] == 1
    assert "SIMULATED" in broker["fill_model"]
    assert broker["outbox"]["counts"]["DEAD"] == 0
    assert api.get(f"/api/v1/paper-orders/{account}").status_code == 404


def test_paper_order_writes_do_not_exist(api, ex):
    seed(ex)
    for method in ("post", "put", "patch", "delete"):
        assert getattr(api, method)("/api/v1/paper-orders").status_code == 405


def printed(capsys) -> Any:
    """The JSON the command printed (structured log lines, if any, are skipped)."""
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line in ("{", "[", "[]"))
    return json.loads("\n".join(lines[start:]))


def test_cli_reads_and_jobs(ex, ctx, capsys, monkeypatch):
    _account, filled, working, _cancelled, _r = seed(ex)
    args = cli._parser().parse_args(["--account", "api-test", "orders", "list", "--group", "open"])
    assert cli.run(ctx, args) == 0
    listed = printed(capsys)
    assert [o["id"] for o in listed] == [str(working)]
    assert cli.run(ctx, cli._parser().parse_args(["orders", "show", "--id", str(filled)])) == 0
    assert printed(capsys)["order"]["status"] == "FILLED"
    assert cli.run(ctx, cli._parser().parse_args(["--account", "api-test", "reconcile"])) == 0
    assert printed(capsys)["passed"] is True
    assert cli.run(ctx, cli._parser().parse_args(["recover"])) == 0
    capsys.readouterr()
    refused = cli._parser().parse_args(
        [
            "cancel",
            "--id",
            str(working),
            "--key",
            "cli-cancel-01",
            "--actor",
            "system:x",
            "--reason",
            "x y z",
        ]
    )
    with pytest.raises(Exception, match="operator action"):
        cli.run(ctx, refused)
    ok = cli._parser().parse_args(
        [
            "cancel",
            "--id",
            str(working),
            "--key",
            "cli-cancel-01",
            "--actor",
            "operator:test",
            "--reason",
            "operator cancellation",
        ]
    )
    assert cli.run(ctx, ok) == 0
    assert printed(capsys)["status"] == "CANCEL_PENDING"
    assert cli.run(ctx, cli._parser().parse_args(["dispatch"])) == 0
    capsys.readouterr()
    assert ex.order(working).status.value == "CANCELLED"
    assert cli.run(ctx, cli._parser().parse_args(["broker", "status"])) == 0
    assert printed(capsys)["simulated"] is True

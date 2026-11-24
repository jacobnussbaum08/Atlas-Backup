"""Phase 7B positions and execution analytics API (GET only) and CLI on PostgreSQL.

TEST FIXTURE market data; simulated positions and fills of CONTROLLED_TEST accounts (read
with ``account_id``). Nothing here can submit, fill or close anything through the API.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.execution import cli
from app.execution.reconciliation import ExecutionReconciler
from app.execution.runtime import ExecutionContext
from app.main import create_app
from app.tests.integration.simulation_support import SESSION, D, Sim, make_simulation

pytestmark = pytest.mark.integration


@pytest.fixture
def sim(fresh_engine) -> Sim:
    return make_simulation(fresh_engine)


@pytest.fixture
def ctx(sim) -> ExecutionContext:
    ex = sim.ex
    return ExecutionContext(
        session_factory=ex.factory,
        clock=ex.risk.clock,
        broker=ex.broker,
        service=ex.service,
        reconciler=ExecutionReconciler(ex.factory, ex.risk.clock),
        simulator=sim.simulator,
        monitor=sim.monitor,
    )


@pytest.fixture
def api(sim, ctx, make_settings):
    app = create_app(make_settings(), clock=sim.clock, health_checks={}, execution=ctx)
    with TestClient(app) as client:
        yield client


def seed(sim: Sim):
    """One closed position (take-profit) and one open position, both simulated."""
    ex = sim.ex
    account = ex.account("api-pos")
    a, b = ex.asset("APX"), ex.asset("APY")
    ra = ex.reservation(account, a, "api-pos-res-1", target=D("101.00"))
    rb = ex.reservation(account, b, "api-pos-res-2")
    ea = ex.open_order(account, ra, "10", "api-pos-order-1")
    eb = ex.open_order(account, rb, "25", "api-pos-order-2")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(a, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.quote(b, "99.50", "99.55", at=SESSION + timedelta(seconds=50), ask_size="20")
    sim.simulate()
    sim.monitor_once()
    ex.dispatch()
    sim.later(30)
    sim.trade(a, "101.05", "300")
    sim.later(1)
    sim.simulate()
    sim.monitor_once()
    ex.dispatch()
    return account, ea, eb


def printed(capsys) -> Any:
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line in ("{", "[", "[]"))
    return json.loads("\n".join(lines[start:]))


def test_position_and_analytics_reads(api, sim):
    account, closed_entry, open_entry = seed(sim)
    q = {"account_id": str(account)}
    rows = api.get("/api/v1/paper-positions", params=q).json()
    assert {r["entry_order_id"] for r in rows} == {str(closed_entry), str(open_entry)}
    assert all(r["simulated"] is True for r in rows)
    (open_row,) = api.get("/api/v1/paper-positions", params=q | {"status": "open"}).json()
    assert open_row["entry_order_id"] == str(open_entry)
    assert D(open_row["open_quantity"]) == D(10)  # 10 of 25 filled (liquidity)
    assert D(open_row["stop_price"]) == D("94.70")
    assert open_row["current_price"] is not None
    assert open_row["holding_seconds"] >= 0
    (closed_row,) = api.get("/api/v1/paper-positions", params=q | {"status": "closed"}).json()
    assert closed_row["exit_reason"] == "TAKE_PROFIT"
    assert D(closed_row["realized_pnl"]) == D("14.20")
    assert D(closed_row["net_pnl"]) < D("14.20")  # net of commissions and fees
    assert closed_row["strategy_id"] is None
    assert closed_row["origin"] == "OPERATOR"

    detail = api.get(f"/api/v1/paper-positions/{closed_row['id']}").json()
    assert "Simulated" in detail["notice"]
    kinds = [e["event_type"] for e in detail["events"]]
    assert kinds[0] == "OPENED"
    assert kinds[-1] == "CLOSED"
    assert {o["purpose"] for o in detail["orders"]} == {"ENTRY", "EXIT"}
    assert all(f["detail"] is not None for f in detail["fills"])
    assert {f["detail"]["basis"] for f in detail["fills"]} == {"QUOTE", "TRADE_THROUGH"}

    summary = api.get("/api/v1/paper-positions/summary", params=q).json()
    assert (summary["open"], summary["closed"], summary["winners"]) == (1, 1, 1)
    assert summary["exit_reasons"] == {"TAKE_PROFIT": 1}

    analytics = api.get("/api/v1/paper-execution/analytics", params=q).json()
    assert analytics["simulated"] is True
    assert "Simulated" in analytics["notice"]
    assert analytics["fills"] == 3
    assert analytics["by_basis"] == {"QUOTE": 2, "TRADE_THROUGH": 1}
    # The passive take-profit fill had no fresh quote: its slippage is unknown, not zero.
    assert analytics["slippage_bps"]["count"] == "2"
    assert D(analytics["costs"]["commission"]) == D("3.00")
    assert analytics["fill_rates"]["TAKE_PROFIT"]["filled"] == 1
    assert analytics["current_model"]["rules"]
    assert any("IEX" in note for note in analytics["limitations"])
    models = api.get("/api/v1/paper-execution/models").json()
    assert len(models) == 1
    assert models[0]["current"] is True
    assert models[0]["simulated"] is True

    order = api.get(f"/api/v1/paper-orders/{open_entry}").json()
    assert order["order"]["status"] == "PARTIALLY_FILLED"
    assert order["execution"]["evaluations"] >= 1
    assert order["execution"]["fill_details"][0]["basis"] == "QUOTE"
    partial = api.get("/api/v1/paper-orders", params=q | {"group": "partially_filled"}).json()
    assert [o["id"] for o in partial] == [str(open_entry)]
    assert api.get(f"/api/v1/paper-positions/{account}").status_code == 404


def test_position_and_execution_writes_do_not_exist(api, sim):
    seed(sim)
    for path in ("/api/v1/paper-positions", "/api/v1/paper-execution/analytics"):
        for method in ("post", "put", "patch", "delete"):
            assert getattr(api, method)(path).status_code == 405


def test_cli_positions_close_and_protect(ctx, sim, capsys):
    account, _closed_entry, _open_entry = seed(sim)
    capsys.readouterr()
    parse = cli._parser().parse_args
    assert (
        cli.run(ctx, parse(["--account", "api-pos", "positions", "list", "--status", "open"])) == 0
    )
    (row,) = printed(capsys)
    position_id = row["id"]
    assert cli.run(ctx, parse(["positions", "show", "--id", position_id])) == 0
    assert printed(capsys)["position"]["status"] == "OPEN"
    assert cli.run(ctx, parse(["--account", "api-pos", "analytics"])) == 0
    assert printed(capsys)["simulated"] is True
    with pytest.raises(Exception, match="operator action"):
        cli.run(
            ctx,
            parse(
                [
                    "protect",
                    "--position-id",
                    position_id,
                    "--stop",
                    "96",
                    "--actor",
                    "system:x",
                    "--reason",
                    "tighten",
                ]
            ),
        )
    assert (
        cli.run(
            ctx,
            parse(
                [
                    "protect",
                    "--position-id",
                    position_id,
                    "--stop",
                    "96.00",
                    "--actor",
                    "operator:test",
                    "--reason",
                    "tighten",
                ]
            ),
        )
        == 0
    )
    assert D(printed(capsys)["stop_price"]) == D("96")
    assert (
        cli.run(
            ctx,
            parse(
                [
                    "close",
                    "--position-id",
                    position_id,
                    "--key",
                    "cli-close-0001",
                    "--actor",
                    "operator:test",
                    "--reason",
                    "flatten",
                ]
            ),
        )
        == 0
    )
    assert printed(capsys)["status"] == "PENDING"
    assert cli.run(ctx, parse(["monitor"])) == 0
    assert printed(capsys)["cancels_requested"] >= 1  # the entry remainder is withdrawn
    assert cli.run(ctx, parse(["simulate"])) == 0
    assert "fills" in printed(capsys)
    assert account is not None

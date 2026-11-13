"""Paper-account Performance (spec §22.6) and notifications (spec §23) on PostgreSQL.

TEST FIXTURE material only (see ``orchestration_support`` and ``simulation_support``): a
CONTROLLED_TEST account, invented instruments, MOCK quotes, a fixture AI decision. The
position below is a simulated paper position of the isolated paper broker. The endpoints are
read-only and derive everything from the stored records.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestration.enums import IntentStatus
from app.tests.integration.orchestration_support import OPERATOR, make_bench
from app.tests.integration.simulation_support import SESSION
from app.tests.integration.test_reliability import sim_of

pytestmark = pytest.mark.integration
D = Decimal


@pytest.fixture
def scenario(fresh_engine, make_settings):
    """One take-profit round trip and one risk-rejected decision on a fresh account."""
    bench = make_bench(fresh_engine, name="perf-notify")
    sim = sim_of(bench)
    ex = bench.ex
    asset = ex.asset("PNQA")
    r = ex.reservation(bench.account, asset, "perf-notify-res", target=D("101.00"))
    entry = ex.open_order(bench.account, r, "10", "perf-notify-order")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    ex.dispatch()
    sim.monitor_once()  # rests the take-profit
    ex.dispatch()
    sim.later(30)
    sim.trade(asset, "101.05", "200")
    sim.later(1)
    sim.simulate()
    sim.cycle(2)
    position = sim.position(entry)
    assert position is not None
    assert position.status.value == "CLOSED"

    other = bench.asset("PNQB")
    bench.decision(other, symbol="PNQB", age=timedelta(minutes=1))
    bench.enable()
    bench.control.risk.service.engage_kill_switch(
        bench.account, actor=OPERATOR, reason="halt for the notification test"
    )
    asyncio.run(bench.orchestrator().orchestrator.tick())
    [intent] = bench.intents()
    assert intent.status is IntentStatus.RISK_REJECTED

    url = fresh_engine.url
    settings = make_settings(
        postgres_host=url.host,
        postgres_port=url.port,
        postgres_db=url.database,
        postgres_user=url.username,
        postgres_password=url.password,
    )
    app = create_app(settings, clock=bench.clock)
    return bench, position, TestClient(app)


def test_performance_reports_the_closed_paper_trade_with_its_sample_size(scenario):
    bench, position, client = scenario
    with client:
        body = client.get(
            "/api/v1/portfolio/performance", params={"account_id": str(bench.account)}
        ).json()
    assert body["simulated"] is True
    assert "Simulated paper trading" in body["disclaimer"]
    trades = body["trades"]
    assert trades["trades"] == 1
    net = position.realized_pnl - position.commissions - position.fees - position.borrow_accrued
    assert D(trades["net_pnl"]) == net.quantize(D("0.01"))
    assert D(trades["costs"]) == (position.commissions + position.fees).quantize(D("0.01"))
    assert trades["wins"] == 1
    assert body["by_exit_reason"][0]["exit_reason"] == "TAKE_PROFIT"
    assert body["by_direction"][0]["direction"] == "LONG"
    [recent] = body["recent_trades"]
    assert recent["exit_reason"] == "TAKE_PROFIT"
    # Ratios are withheld below their minimum sample, with the reason.
    summary = body["summary"]
    assert summary["sharpe"] is None
    assert summary["reason"]
    assert body["curve"]["days"] == len(body["curve"]["points"])
    assert body["benchmark"]["return"] is None  # no SPY bars in this database
    assert body["benchmark"]["reason"]


def test_notifications_list_positions_rejections_and_breakers_and_page_forward(scenario):
    bench, position, client = scenario
    with client:
        url = "/api/v1/notifications"
        body = client.get(url, params={"account_id": str(bench.account)}).json()
        kinds = [n["kind"] for n in body["items"]]
        assert "POSITION_OPENED" in kinds
        assert "POSITION_CLOSED" in kinds
        assert "TRADE_REJECTED" in kinds
        assert all(n["simulated"] for n in body["items"])
        times = [n["occurred_at"] for n in body["items"]]
        assert times == sorted(times, reverse=True)  # newest first
        assert len({n["id"] for n in body["items"]}) == len(body["items"])  # stable, unique
        closed = next(n for n in body["items"] if n["kind"] == "POSITION_CLOSED")
        assert closed["subject"]["position_id"] == str(position.id)
        assert closed["subject"]["exit_reason"] == "TAKE_PROFIT"
        rejected = next(n for n in body["items"] if n["kind"] == "TRADE_REJECTED")
        assert rejected["subject"]["symbol"] == "PNQB"

        only = client.get(
            url, params={"account_id": str(bench.account), "kind": "POSITION_CLOSED"}
        ).json()["items"]
        assert [n["kind"] for n in only] == ["POSITION_CLOSED"]
        newest = body["items"][0]["occurred_at"]
        later = client.get(url, params={"account_id": str(bench.account), "since": newest})
        assert later.json()["items"] == []
        assert client.get(url, params={"kind": "NOT_A_KIND"}).status_code == 422

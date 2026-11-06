"""Phase 6B Risk Center API and CLI against a real PostgreSQL (TEST FIXTURE data)."""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.risk import cli
from app.risk.runtime import RiskContext
from app.tests.integration.risk_support import OPERATOR, Risk, make_risk

pytestmark = pytest.mark.integration


@pytest.fixture
def risk(fresh_engine) -> Risk:
    return make_risk(fresh_engine)


@pytest.fixture
def ctx(risk) -> RiskContext:
    return RiskContext(
        session_factory=risk.factory,
        clock=risk.clock,
        accounting=risk.service._accounting,
        service=risk.service,
    )


@pytest.fixture
def api(risk, ctx, make_settings):
    app = create_app(
        make_settings(), clock=risk.clock, health_checks={}, accounting=risk.books.ctx, risk=ctx
    )
    with TestClient(app) as client:
        yield client


def test_without_a_primary_account_the_api_says_so(api):
    response = api.get("/api/v1/risk/state")
    assert response.status_code == 404
    assert "cli init" in response.json()["error"]["message"]


def test_state_limits_exposure_and_history(api, risk):
    primary = risk.books.account("Atlas paper account", test=False)
    unconfigured = api.get("/api/v1/risk/limits").json()
    assert unconfigured["configured"] is False
    assert "risk.cli init" in unconfigured["message"]
    risk.configure(primary)
    risk.reconcile(primary)
    asset = risk.asset("RSKA")
    assessed = risk.assess(primary, risk.proposal(asset, "99.70", "94.70"))
    risk.assess(primary, risk.proposal(asset, "99.70", None))

    state = api.get("/api/v1/risk/state").json()
    assert state["trading_halted"] is False
    assert state["kill_switch"] == "CLEAR"
    assert {b["breaker"] for b in state["breakers"]} >= {"DAILY_LOSS", "KILL_SWITCH"}
    assert state["live"]["recorded"] is False
    assert state["live"]["mark_status"] == "COMPLETE"
    assert D(state["live"]["equity"]) == D("100000.00")
    assert state["reserved_exposure"]["amount"] == "0"
    assert state["assessment_counts"]["PASS_FOR_RESERVATION"] == 1
    assert state["assessment_counts"]["REJECTED"] == 1
    assert "not optimal" in state["limits"]["note"]

    limits = api.get("/api/v1/risk/limits").json()
    assert limits["active"]["version"] == 1
    assert limits["active"]["hard_limits"]["risk_per_trade_pct"] == "0.005"
    assert limits["breaker_recovery"]["DATA_QUALITY"] == "AUTO_WHEN_CLEAR"

    exposure = api.get("/api/v1/risk/exposure").json()
    assert D(exposure["gross_exposure"]) == 0
    assert exposure["baselines"]["DAILY"]["basis"] == "FUNDED_IN_PERIOD"

    listed = api.get("/api/v1/risk/assessments").json()
    assert sorted(a["result"] for a in listed) == ["PASS_FOR_RESERVATION", "REJECTED"]
    rejected = api.get("/api/v1/risk/assessments", params={"result": "REJECTED"}).json()
    assert rejected[0]["reasons"] == ["MISSING_STOP"]
    detail = api.get(f"/api/v1/risk/assessments/{assessed.assessment_id}").json()
    assert detail["permitted_quantity"] is not None
    assert D(detail["permitted_quantity"]) == D("92")
    assert "not an execution approval" in detail["note"]
    assert detail["checks"][0]["check"] == "circuit_breakers"
    assert api.get("/api/v1/risk/configurations").json()[0]["version"] == 1
    assert len(api.get("/api/v1/risk/exposure/snapshots").json()) == 2
    assert api.get("/api/v1/risk/events").json()


def test_kill_switch_engage_only_halts(api, risk):
    primary = risk.books.account("Atlas paper account", test=False)
    risk.configure(primary)
    risk.reconcile(primary)
    response = api.post("/api/v1/risk/kill-switch/engage", json={"reason": "dashboard halt"})
    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is True
    assert body["already_engaged"] is False
    assert "kill-switch release" in body["release"]
    again = api.post("/api/v1/risk/kill-switch/engage", json={"reason": "again"}).json()
    assert again["already_engaged"] is True
    state = api.get("/api/v1/risk/state").json()
    assert state["trading_halted"] is True
    assert state["active_breakers"] == ["KILL_SWITCH"]
    events = api.get("/api/v1/risk/circuit-breakers/events").json()
    assert [(e["breaker"], e["event"], e["actor"]) for e in events] == [
        ("KILL_SWITCH", "TRIPPED", "operator:api")
    ]
    assert api.post("/api/v1/risk/kill-switch/engage", json={"reason": ""}).status_code == 422


def test_cli_commands(risk, ctx, capsys):
    account = risk.books.account("Atlas paper account", test=False)
    risk.reconcile(account)
    risk.asset("RSKA")

    def run(*argv: str) -> tuple[int, Any]:
        code = cli.run(ctx, cli._parser().parse_args(list(argv)))
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    code, out = run("init", "--actor", OPERATOR, "--reason", "starting values for the test")
    assert code == 0
    assert out["version"] == 1
    assert out["created"] is True
    code, out = run("init", "--actor", OPERATOR, "--reason", "starting values for the test")
    assert out["created"] is False
    code, out = run(
        "set",
        "--actor",
        OPERATOR,
        "--reason",
        "six positions at most for the test",
        "--hard",
        "max_open_positions=6",
        "--pref",
        "slippage_bps=8",
    )
    assert out["version"] == 2
    assert {c["field"] for c in out["changes"]} == {"max_open_positions", "slippage_bps"}
    code, out = run(
        "classify",
        "--actor",
        OPERATOR,
        "--symbol",
        "RSKA",
        "--sector",
        "semiconductors",
        "--cluster",
        "chips",
        "--provenance",
        "operator judgement (test)",
    )
    assert code == 0
    code, out = run(
        "assess", "--symbol", "RSKA", "--direction", "LONG", "--entry", "99.70", "--stop", "94.70"
    )
    assert out["result"] == "PASS_FOR_RESERVATION"
    assessment = out["id"]
    code, out = run("replay", "--assessment", assessment)
    assert out == {"reproduced": True, "result": "PASS_FOR_RESERVATION"}
    code, out = run(
        "assess", "--symbol", "RSKA", "--direction", "LONG", "--entry", "99.70", "--strict"
    )
    assert code == 1
    assert out["result"] == "REJECTED"
    code, out = run("kill-switch", "engage", "--actor", OPERATOR, "--reason", "cli halt test")
    assert out["kill_switch"] == "TRIPPED"
    code, out = run("monitor")
    assert out["active_breakers"] == ["KILL_SWITCH"]
    code, out = run("show")
    assert out["trading_halted"] is True
    code, out = run("kill-switch", "release", "--actor", OPERATOR, "--reason", "cli resume test")
    assert out["kill_switch"] == "CLEAR"
    with pytest.raises(Exception, match="not an authorized operator"):
        run("set", "--actor", "ai:coordinator", "--reason", "the model asks", "--hard", "x=1")

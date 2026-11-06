"""Phase 6C reservation API (read-only) and CLI against a real PostgreSQL (TEST FIXTURE)."""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.risk import cli
from app.risk.enums import ReleaseResolution
from app.risk.runtime import RiskContext
from app.tests.integration.reservation_support import Reservations, make_reservations

pytestmark = pytest.mark.integration


@pytest.fixture
def rv(fresh_engine) -> Reservations:
    return make_reservations(fresh_engine)


@pytest.fixture
def ctx(rv) -> RiskContext:
    return RiskContext(
        session_factory=rv.factory,
        clock=rv.risk.clock,
        accounting=rv.risk.service._accounting,
        service=rv.risk.service,
        reservations=rv.service,
        reconciler=rv.reconciler,
    )


@pytest.fixture
def api(rv, ctx, make_settings):
    app = create_app(
        make_settings(),
        clock=rv.risk.clock,
        health_checks={},
        accounting=rv.risk.books.ctx,
        risk=ctx,
    )
    with TestClient(app) as client:
        yield client


def primary(rv: Reservations):
    risk = rv.risk
    account = risk.books.account("Atlas paper account", test=False)
    risk.configure(account)
    risk.reconcile(account)
    return account


def test_reservation_reads(api, rv):
    risk = rv.risk
    account = primary(rv)
    a, b = risk.asset("APIA", sector="S_A"), risk.asset("APIB", sector="S_B")
    held = rv.reserve(account, risk.proposal(a, "99.70", "94.70"), "api-reserve-1")
    gone = rv.reserve(account, risk.proposal(b, "99.70", "94.70"), "api-reserve-2")
    rv.reserve(account, risk.proposal(b, "99.70", None), "api-reserve-3")  # rejected
    rv.service.release(
        gone.reservation_id,
        resolution=ReleaseResolution.NOT_SUBMITTED,
        idempotency_key="api-release-2",
        actor="system:test-worker",
        reason="not submitted (controlled test event)",
    )

    cap = api.get("/api/v1/risk/reservations/capacity").json()
    assert cap["reserved"]["active_count"] == 1
    assert D(cap["reserved"]["risk"]) == D("499.45")
    assert D(cap["reserved"]["gross_exposure"]) == D("9188.46")
    assert D(cap["available"]["risk_budget"]) == D("2000.00") - D("499.45")
    assert D(cap["available"]["gross_exposure"]) == D("100000") - D("9188.46")
    assert cap["counts"]["APPROVED"] == 1
    assert cap["counts"]["RELEASED"] == 1
    assert cap["counts"]["REJECTED"] == 1
    assert cap["approvals_blocked"] is False

    state = api.get("/api/v1/risk/state").json()
    assert D(state["reserved_exposure"]["reserved"]["risk"]) == D("499.45")
    assert D(state["live"]["pending_exposure"]) == D("9188.46")

    active = api.get("/api/v1/risk/reservations", params={"active": "true"}).json()
    assert [r["id"] for r in active] == [str(held.reservation_id)]
    assert active[0]["approved_quantity"] == "92"
    everything = api.get("/api/v1/risk/reservations").json()
    assert len(everything) == 3
    rejected = api.get("/api/v1/risk/reservations", params={"status": "REJECTED"}).json()
    assert rejected[0]["reasons"]

    detail = api.get(f"/api/v1/risk/reservations/{held.reservation_id}").json()
    assert [e["event_type"] for e in detail["events"]] == ["CREATED", "APPROVED"]
    assert detail["approval"]["approved_quantity"] == "92"
    assert detail["assessment"]["result"] == "PASS_FOR_RESERVATION"
    assert detail["requests"][0]["command"] == "RESERVE"
    assert "not an order" in detail["note"]

    releases = api.get("/api/v1/risk/reservations/events", params={"event_type": "RELEASED"}).json()
    assert len(releases) == 1 and releases[0]["resolution"] == "NOT_SUBMITTED"  # noqa: PT018
    assert api.get("/api/v1/risk/reservations/reconciliations").json() == []
    rv.reconciler.run(account, actor="system:test")
    runs = api.get("/api/v1/risk/reservations/reconciliations").json()
    assert runs[0]["status"] == "PASSED"
    assert api.get(f"/api/v1/risk/reservations/{held.reservation_id}x").status_code == 422


def test_no_reservation_write_exists_in_the_api(api):
    paths = api.get("/openapi.json").json()["paths"]
    writes = {p for p, methods in paths.items() if p.startswith("/api/v1/risk/reservations")}
    assert writes
    assert all(set(paths[p]) == {"get"} for p in writes)


def test_reservation_cli(rv, ctx, capsys):
    risk = rv.risk
    account = primary(rv)
    asset = risk.asset("CLIR")

    def run(*argv: str) -> tuple[int, Any]:
        code = cli.run(ctx, cli._parser().parse_args(list(argv)))
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    args = ["--symbol", "CLIR", "--direction", "LONG", "--entry", "99.70", "--stop", "94.70"]
    code, out = run("reserve", *args, "--idempotency-key", "cli-key-0001", "--strict")
    assert code == 0 and out["decision"] == "APPROVED" and out["created"] is True  # noqa: PT018
    code, again = run("reserve", *args, "--idempotency-key", "cli-key-0001")
    assert again["id"] == out["id"] and again["created"] is False  # noqa: PT018
    code, listed = run("reservations", "list", "--active")
    assert [r["id"] for r in listed] == [out["id"]]
    code, cap = run("reservations", "capacity")
    assert D(cap["reserved"]["risk"]) == D("499.45")
    code, rec = run("reservations", "reconcile")
    assert code == 0 and rec["status"] == "PASSED"  # noqa: PT018
    code, released = run(
        "reservations",
        "release",
        "--id",
        out["id"],
        "--resolution",
        "OPERATOR",
        "--key",
        "cli-release-01",
        "--actor",
        "operator:test",
        "--reason",
        "operator review",
    )
    assert released["status"] == "RELEASED"
    code, recovered = run("reservations", "recover")
    assert code == 0 and recovered["status"] == "PASSED"  # noqa: PT018
    assert asset is not None
    assert account is not None

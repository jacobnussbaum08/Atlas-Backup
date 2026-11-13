"""Phase 6A portfolio/accounting API and CLI against a real PostgreSQL (TEST FIXTURE data)."""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.accounting import cli
from app.main import create_app
from app.tests.integration.accounting_support import Books, later, make_books

pytestmark = pytest.mark.integration


@pytest.fixture
def books(fresh_engine) -> Books:
    return make_books(fresh_engine)


@pytest.fixture
def api(books, make_settings):
    app = create_app(make_settings(), clock=books.clock, health_checks={}, accounting=books.ctx)
    with TestClient(app) as client:
        yield client


def test_not_initialized_is_explicit(api):
    response = api.get("/api/v1/portfolio/overview")
    assert response.status_code == 404
    assert "cli init" in response.json()["error"]["message"]
    assert api.get("/api/v1/portfolio/accounts").json() == []


def test_primary_account_overview_shows_funding_separately(api, books):
    primary = books.account("Atlas paper account", test=False)
    books.ctx.reconciliation.run(primary, actor="test")
    body = api.get("/api/v1/portfolio/overview").json()
    assert body["account"]["purpose"] == "PRIMARY"
    assert D(body["account"]["opening_funding"]) == D("100000.00")
    assert D(body["figures"]["equity"]) == D("100000.00")
    assert D(body["figures"]["contributed_capital"]) == D("100000.00")
    assert D(body["figures"]["investment_return"]) == 0
    assert body["positions"] == {"long": 0, "short": 0}
    assert body["reconciliation"]["status"] == "PASSED"
    assert body["reconciliation"]["current"] is True
    assert body["accounting_errors"] == []
    assert "not a return" in body["disclaimer"]
    cash = api.get("/api/v1/portfolio/cash").json()
    assert cash["currencies"][0]["currency"] == "USD"
    assert D(cash["currencies"][0]["cash"]) == D("100000.00")
    trial = api.get("/api/v1/accounting/balances").json()
    assert trial["balanced"] is True
    assert {(r["account_code"], D(r["balance"])) for r in trial["rows"]} == {
        ("1000", D("100000.00")),
        ("3000", D("100000.00")),
    }
    journal = api.get("/api/v1/accounting/journal").json()
    assert [(e["sequence"], e["entry_type"], e["template"]) for e in journal] == [
        (1, "OPENING_FUNDING", "opening_funding.v1")
    ]
    detail = api.get(f"/api/v1/accounting/journal/{journal[0]['id']}").json()
    assert [
        (line["account_code"], D(line["debit"]), D(line["credit"])) for line in detail["lines"]
    ] == [
        ("1000", D("100000.00"), D(0)),
        ("3000", D(0), D("100000.00")),
    ]
    assert len(api.get("/api/v1/accounting/chart").json()) == 19
    assert [t["code"] for t in api.get("/api/v1/accounting/templates").json()] == [
        "opening_funding"
    ]
    runs = api.get("/api/v1/accounting/reconciliation").json()
    run = api.get(f"/api/v1/accounting/reconciliation/{runs[0]['id']}").json()
    assert run["run"]["status"] == "PASSED"
    assert all(r["status"] == "PASS" for r in run["results"])


def test_test_account_holdings_pnl_and_snapshots(api, books):
    account = books.account("Controlled test A")
    aaa = books.asset("AAA", bars=(150.0, 0.0))
    books.trade(account, "long_purchase", "buy", aaa, "100", "150.25", "1.00", "0.05")
    books.ctx.valuation.checkpoint(account, actor="test")
    later(books, 5)
    books.trade(account, "long_sale", "sell", aaa, "40", "151.00")
    query = {"account_id": str(account)}
    holdings = api.get("/api/v1/portfolio/holdings", params=query).json()
    assert len(holdings) == 1
    held = holdings[0]
    assert (held["symbol"], held["side"], D(held["quantity"])) == ("AAA", "LONG", D(60))
    assert D(held["cost_basis"]) == D("9015.00")
    assert D(held["valuation_adjustment"]) == D("-33.00")
    assert D(held["realized_pnl"]) == D("30.00")
    assert held["last_price_source"] == "BAR_CLOSE"
    pnl = api.get("/api/v1/portfolio/pnl", params=query).json()
    assert D(pnl["totals"]["realized_pnl"]) == D("30.00")
    assert D(pnl["totals"]["unrealized_pnl"]) == D("-33.00")
    assert D(pnl["totals"]["total_costs"]) == D("1.05")
    by_asset = pnl["by_asset"][0]
    assert (by_asset["symbol"], D(by_asset["realized"]), D(by_asset["commissions"])) == (
        "AAA",
        D("30.00"),
        D("1.00"),
    )
    snapshots = api.get("/api/v1/portfolio/snapshots", params=query).json()
    assert len(snapshots) == 1
    assert snapshots[0]["valuation_status"] == "COMPLETE"
    overview = api.get("/api/v1/portfolio/overview", params=query).json()
    assert overview["latest_snapshot"]["entries_since"] == 1  # the sale came after it
    assert overview["account"]["purpose"] == "CONTROLLED_TEST"
    accounts = api.get("/api/v1/portfolio/accounts").json()
    assert [a["purpose"] for a in accounts] == ["CONTROLLED_TEST"]
    assert api.get("/api/v1/accounting/fx-rates").json() == []


def test_accounting_errors_surface_in_the_overview(api, books, fresh_engine):
    from sqlalchemy import text

    primary = books.account("Atlas paper account", test=False)
    with fresh_engine.begin() as conn:
        conn.execute(text("UPDATE currency_balances SET line_count = line_count + 1"))
    books.ctx.reconciliation.run(primary, actor="test")
    body = api.get("/api/v1/portfolio/overview").json()
    assert body["reconciliation"]["status"] == "FAILED"
    assert body["accounting_errors"]
    assert {e["severity"] for e in body["accounting_errors"]} == {"HIGH"}


def test_cli_commands(books, capsys, tmp_path):
    def run(*argv: str) -> tuple[int, Any]:
        code = cli.run(books.ctx, cli._parser().parse_args(list(argv)))
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    code, out = run("init")
    assert code == 0
    assert out["funding_posted"] is True
    later(books, 60)
    code, out = run("init")
    assert out["funding_posted"] is False  # idempotent, whenever it is re-run
    code, _ = run("create-test-account", "--name", "CLI test", "--funding", "5000.00")
    assert code == 0
    asset = books.asset("CLI")
    params = tmp_path / "buy.json"
    params.write_text(json.dumps({"asset_id": str(asset), "quantity": "2", "price": "10.00"}))
    code, out = run(
        "post",
        "--account",
        "CLI test",
        "--template",
        "long_purchase",
        "--event-id",
        "cli-buy",
        "--params",
        f"@{params}",
    )
    assert code == 0
    entry = out["entry_id"]
    code, out = run(
        "reverse", "--account", "CLI test", "--entry", str(entry), "--reason", "cli test"
    )
    assert out["entry_type"] == "REVERSAL"
    code, out = run(
        "fx-rate",
        "--base",
        "EUR",
        "--quote",
        "USD",
        "--rate",
        "1.08",
        "--observed-at",
        books.clock.now().isoformat(),
        "--source",
        "TEST_FIXTURE",
        "--provenance",
        "cli fixture",
    )
    assert code == 0
    code, out = run("checkpoint", "--account", "CLI test")
    assert code == 0
    assert out["reconciliation"] == "PASSED"
    code, out = run("reconcile")
    assert (code, out["status"]) == (0, "PASSED")
    code, out = run("show")
    assert D(out["figures"]["equity"]) == D("100000.00")
    with pytest.raises(Exception, match="CONTROLLED_TEST"):
        run(
            "post",
            "--account",
            "Atlas paper account",
            "--template",
            "fee",
            "--event-id",
            "x",
            "--params",
            '{"kind": "FEE", "currency": "USD", "amount": "1"}',
        )

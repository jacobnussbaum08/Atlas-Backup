"""Asset discovery API against real PostgreSQL (TEST FIXTURE directory, MOCK bars)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.asset_discovery.enums import DiscoveryJobKind
from app.asset_discovery.runtime import AssetDiscoveryContext
from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.runtime import MarketDataContext
from app.database.models import AuditLog
from app.database.sessions import session_scope
from app.main import create_app
from app.tests.fixtures.alpaca_assets import asset_record
from app.tests.integration.asset_discovery_support import make_env, seed_daily_bars

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    env = make_env(fresh_engine)
    env.directory.set(
        asset_record("LIQ", attributes=("has_options",)),
        asset_record("LIQ2", exchange="NYSE"),
        asset_record("CHEAP"),
        asset_record("OTCX", exchange="OTC"),
        asset_record("GONE", status="inactive", tradable=False),
    )
    env.discover()
    env.clock.advance(timedelta(minutes=5))
    seed_daily_bars(env, "LIQ", close="40", volume=500_000)
    seed_daily_bars(env, "LIQ2", close="60", volume=300_000)
    seed_daily_bars(env, "CHEAP", close="2", volume=900_000)
    env.run(DiscoveryJobKind.QUALIFICATION, pipeline=True, pipeline_step=0, prepare_history=False)
    return env


@pytest.fixture
def client(env, make_settings):
    def build(**settings) -> TestClient:
        provider = env.service._provider
        market = MarketDataSettings(_env_file=None)
        context = MarketDataContext(
            settings=market,
            provider=provider,
            service=HistoricalIngestionService(env.factory, provider, market, env.clock),
            session_factory=env.factory,
        )
        app = create_app(
            make_settings(**settings),
            clock=FixedClock(env.clock.now()),
            health_checks={},
            market_data=context,
            asset_discovery=AssetDiscoveryContext(
                env.service.settings, provider, env.service, env.factory, env.clock
            ),
        )
        return TestClient(app)

    return build


def test_summary_counts_layers_and_jobs(client):
    body = client().get("/api/v1/discovery/summary").json()
    assert body["catalogued"] == 5
    assert body["discovery_pool"] == 4
    assert body["qualified"] == 2
    assert body["watchlist"] == 2
    assert body["by_status"]["INACTIVE"] == 1
    assert body["last_successful_discovery"]["status"] == "COMPLETED"
    assert body["last_jobs"]["WATCHLIST_SCORING"]["status"] == "COMPLETED"
    assert "instrument type" in " ".join(body["reference_capabilities"]["not_supplied"])


def test_asset_lists_filter_sort_and_paginate(client):
    c = client()
    page = c.get("/api/v1/assets", params={"limit": 2, "offset": 0, "sort": "symbol"}).json()
    assert page["total"] == 5
    assert [a["symbol"] for a in page["items"]] == ["CHEAP", "GONE"]
    pool = c.get("/api/v1/assets/discovery-pool").json()
    assert {a["symbol"] for a in pool["items"]} == {"LIQ", "LIQ2", "CHEAP", "OTCX"}
    qualified = c.get("/api/v1/assets/qualified").json()
    assert {a["symbol"] for a in qualified["items"]} == {"LIQ", "LIQ2"}
    assert all(a["qualification_outcome"] == "QUALIFIED" for a in qualified["items"])
    watch = c.get("/api/v1/assets/watchlist").json()
    assert [a["watchlist_rank"] for a in watch["items"]] == [1, 2]
    assert "not a trading signal" in " ".join(watch["notes"])
    search = c.get("/api/v1/assets", params={"search": "liq2"}).json()
    assert [a["symbol"] for a in search["items"]] == ["LIQ2"]
    nyse = c.get("/api/v1/assets", params={"exchange": "xnys"}).json()
    assert [a["symbol"] for a in nyse["items"]] == ["LIQ2"]
    disq = c.get("/api/v1/assets", params={"outcome": "DISQUALIFIED"}).json()
    assert {a["symbol"] for a in disq["items"]} == {"CHEAP", "OTCX"}
    inactive = c.get("/api/v1/assets", params={"status": "INACTIVE"}).json()
    assert [a["symbol"] for a in inactive["items"]] == ["GONE"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"sort": "price"},
        {"order": "sideways"},
        {"offset": -1},
        {"status": "MAYBE"},
    ],
)
def test_list_parameters_are_validated(client, params):
    assert client().get("/api/v1/assets", params=params).status_code == 422


def test_asset_detail_qualification_and_history(client, env):
    c = client()
    liq = env.asset_id("LIQ")
    detail = c.get(f"/api/v1/assets/{liq}").json()
    assert detail["asset"]["symbol"] == "LIQ"
    assert detail["reference"]["tradable"] is True
    assert detail["reference"]["attributes"] == ["has_options"]
    assert detail["mappings"][0]["valid_from_basis"] == "PROVIDER_HISTORY_ASSUMED"
    assert {s["layer"]: s["member"] for s in detail["layers"]} == {
        "DISCOVERY_POOL": True,
        "QUALIFIED_UNIVERSE": True,
        "ACTIVE_WATCHLIST": True,
    }
    assert detail["latest_evaluation"]["outcome"] == "QUALIFIED"
    assert detail["latest_score"]["rank"] in (1, 2)
    assert detail["latest_evaluation"]["rule_set_code"] == "us-equity-iex"
    cheap = c.get(f"/api/v1/assets/{env.asset_id('CHEAP')}/qualification").json()
    evaluation = cheap["evaluations"][0]
    assert evaluation["outcome"] == "DISQUALIFIED"
    failed = {r["rule"]: r["message"] for r in evaluation["rule_results"] if r["status"] == "FAIL"}
    assert "fails >= 5 USD" in failed["min_price"]
    assert {m["metric"] for m in cheap["metrics"]} >= {"avg_daily_volume", "history_coverage"}
    history = c.get(f"/api/v1/assets/{liq}/history").json()
    assert [e["event_type"] for e in history["lifecycle"]] == ["FIRST_DISCOVERED"]
    assert {(e["layer"], e["action"]) for e in history["membership"]} == {
        ("DISCOVERY_POOL", "ADDED"),
        ("QUALIFIED_UNIVERSE", "ADDED"),
        ("ACTIVE_WATCHLIST", "ADDED"),
    }
    assert history["reference_versions"][0]["version"] == 1
    assert c.get("/api/v1/assets/00000000-0000-0000-0000-000000000000").status_code == 404
    assert c.get("/api/v1/assets/not-a-uuid").status_code == 422


def test_jobs_job_detail_and_rankings(client, env):
    c = client()
    page = c.get("/api/v1/discovery/jobs").json()
    kinds = [j["kind"] for j in page["items"]]
    assert set(kinds) == {k.value for k in DiscoveryJobKind} - {"METADATA_REFRESH"}
    discovery = c.get("/api/v1/discovery/jobs", params={"kind": "DIRECTORY_DISCOVERY"}).json()
    detail = c.get(f"/api/v1/discovery/jobs/{discovery['items'][0]['id']}").json()
    assert detail["job"]["counters"]["new"] == 5
    qualification = env.latest(DiscoveryJobKind.QUALIFICATION)
    chain = c.get(f"/api/v1/discovery/jobs/{qualification.id}").json()
    assert [j["kind"] for j in chain["children"]] == ["MEMBERSHIP_UPDATE"]
    rankings = c.get("/api/v1/discovery/rankings").json()
    assert [s["rank"] for s in rankings["items"]] == [1, 2]
    assert rankings["items"][0]["factor_scores"]["liquidity"]["available"] is True
    assert "not a trading signal" in rankings["notes"][0]
    assert c.get(f"/api/v1/discovery/jobs/{qualification.id}x").status_code == 422


def test_market_sessions_per_venue(client):
    body = client().get("/api/v1/market-sessions", params={"at": "2026-11-26T16:00:00Z"}).json()
    venues = {v["mic"]: v for v in body["exchanges"]}
    assert venues["XNAS"]["closure"] == "Thanksgiving Day"
    assert venues["XNAS"]["session"] == "CLOSED"
    assert venues["XNAS"]["provider_codes"] == ["alpaca:NASDAQ"]
    assert venues["ARCX"]["provider_codes"] == ["alpaca:ARCA", "alpaca:NYSEARCA"]
    assert venues["OTCM"]["supported"] is False
    assert venues["XNYS"]["halts_known"] is False


def test_operational_endpoints_need_the_operator_token(client, env):
    guarded = client(operator_token="op-token-1234567890")
    assert guarded.post("/api/v1/discovery/run", json={}).status_code == 401
    response = guarded.post(
        "/api/v1/discovery/run",
        json={"mode": "FULL", "pipeline": False},
        headers={"X-Atlas-Operator-Token": "op-token-1234567890"},
    )
    assert response.status_code == 202
    assert response.json()["created"] is True
    assert response.json()["job"]["kind"] == "DIRECTORY_DISCOVERY"
    assert (
        guarded.post(
            "/api/v1/discovery/refresh",
            json={"max_assets": 0},
            headers={"X-Atlas-Operator-Token": "op-token-1234567890"},
        ).status_code
        == 422
    )
    assert (
        guarded.post(
            "/api/v1/discovery/run",
            json={"mode": "SOMETIMES"},
            headers={"X-Atlas-Operator-Token": "op-token-1234567890"},
        ).status_code
        == 422
    )


def test_repeated_run_requests_return_the_active_job(client, env):
    c = client()
    queued = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=env_trigger(),
        requested_by="t",
        params={"mode": "FULL"},
    )
    response = c.post("/api/v1/discovery/run", json={"mode": "FULL"})
    assert response.status_code == 200
    assert response.json() == {"job": response.json()["job"], "created": False}
    assert response.json()["job"]["id"] == str(queued.job.id)


def test_configuration_is_versioned_and_audited(client, env):
    c = client()
    config = c.get("/api/v1/discovery/config").json()
    assert config["rule_set"]["code"] == "us-equity-iex"
    assert config["rule_set"]["version"] == 1
    new_rules = config["rule_set"]["config"] | {"min_price": "7"}
    created = c.post(
        "/api/v1/discovery/config/rule-set",
        json={
            "description": "raise the price floor (test)",
            "reason": "test",
            "config": new_rules,
        },
    )
    assert created.status_code == 201
    assert created.json()["version"] == 2
    wrong_feed = c.post(
        "/api/v1/discovery/config/rule-set",
        json={
            "description": "sip thresholds",
            "reason": "test",
            "config": new_rules | {"feed": "sip"},
        },
    )
    assert wrong_feed.status_code == 422
    scoring = config["scoring"]["config"] | {"watchlist_size": 1}
    assert (
        c.post(
            "/api/v1/discovery/config/scoring",
            json={
                "description": "one-asset watchlist (test)",
                "reason": "test",
                "config": scoring,
            },
        ).status_code
        == 201
    )
    bad = config["scoring"]["config"] | {"weights": {"profit": "1"}}
    assert (
        c.post(
            "/api/v1/discovery/config/scoring",
            json={
                "description": "bad",
                "reason": "test",
                "config": bad,
            },
        ).status_code
        == 422
    )
    with session_scope(env.factory) as session:
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.actor_id == "operator")
        ).all()
    assert "qualification_rule_sets.version_created" in actions
    assert "scoring_versions.version_created" in actions


def test_no_order_or_trading_routes_exist(client):
    schema = client().app.openapi()["paths"]
    paths = set(schema)
    assert "/api/v1/discovery/run" in paths
    # Phase 7A: simulated paper orders and the simulated paper broker are read-only records
    # (GET only); nothing anywhere submits, cancels or executes an order.
    paper = {
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
    for path in paper:
        assert set(schema[path]) == {"get"}
    for path in paths - paper:
        assert not any(word in path for word in ("order", "trade-execution", "position", "broker"))


def env_trigger():
    from app.asset_discovery.enums import JobTrigger

    return JobTrigger.API

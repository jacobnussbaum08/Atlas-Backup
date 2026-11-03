"""Strategy Laboratory API against real PostgreSQL (Phase 4A).

The catalog is the real six families plus a TEST FIXTURE strategy that can propose
(``strategy_support``); bars are MOCK observations.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.strategies.registry import StrategyRegistry, discover
from app.strategies.runtime import StrategyContext
from app.tests.integration.strategy_support import (
    FailingStrategy,
    ProposingStrategy,
    evaluation_request,
    make_asset,
    make_lab,
    seed_bars,
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/strategies"


@pytest.fixture
def lab(fresh_engine):
    return make_lab(fresh_engine, [*discover(), ProposingStrategy(), FailingStrategy()])


@pytest.fixture
def client(lab, make_settings):
    def build(**settings) -> TestClient:
        ctx = StrategyContext(lab.engine.settings, lab.registry, lab.engine, lab.factory, lab.clock)
        app = create_app(
            make_settings(**settings), clock=lab.clock, health_checks={}, strategies=ctx
        )
        return TestClient(app)

    return build


@pytest.fixture
def evaluated(lab):
    asset = make_asset(lab.factory, "FXA")
    seed_bars(lab.factory, asset, count=40)
    ok = lab.engine.evaluate(evaluation_request(lab, asset))
    failed = lab.engine.evaluate(evaluation_request(lab, asset, "fixture_failing"))
    return asset, ok, failed


def test_catalog_lists_every_strategy_without_performance_figures(client):
    with client() as c:
        body = c.get(BASE).json()
    ids = [s["strategy_id"] for s in body["strategies"]]
    assert set(ids) >= {
        "momentum",
        "trend_following",
        "mean_reversion",
        "breakout",
        "volatility",
        "event_driven",
    }
    momentum = next(s for s in body["strategies"] if s["strategy_id"] == "momentum")
    assert momentum["implemented"] is False
    assert momentum["enabled"] is False
    assert "Phase 4B" in momentum["implementation_note"]
    assert momentum["current_version"]["version"] == 1
    assert momentum["supported_asset_classes"] == ["EQUITY", "ETF"]
    assert momentum["supported_timeframes"] == ["1Day", "1Week"]
    assert momentum["configured_timeframes"] == ["1Day"]
    assert momentum["schedule"]["triggers"] == ["BAR_CLOSE"]
    event = next(s for s in body["strategies"] if s["strategy_id"] == "event_driven")
    assert event["schedule"]["triggers"] == ["BAR_CLOSE", "NEWS_EVENT"]
    text = str(body).lower()
    for word in ("win_rate", "profit", "sharpe", "drawdown", "pnl", "expectancy"):
        assert word not in text
    assert body["sync_problems"] == []
    assert "not a probability" in body["notes"][0]


def test_strategy_detail_versions_and_configurations(client):
    with client() as c:
        detail = c.get(f"{BASE}/breakout").json()
        versions = c.get(f"{BASE}/breakout/versions").json()
        configs = c.get(f"{BASE}/breakout/configurations").json()
        missing = c.get(f"{BASE}/no_such_strategy")
    assert detail["parameters"]["channel_length"] == 20
    assert detail["parameters_schema"]["properties"]["channel_length"]["minimum"] == 5
    assert [v["version"] for v in versions] == [1]
    assert configs[0]["versions"] == [1]
    assert missing.status_code == 404


def test_operator_creates_a_version_and_cannot_enable_interface_only(client):
    with client(operator_token="op-secret") as c:
        unauthorized = c.post(
            f"{BASE}/breakout/versions", json={"parameters": {}, "reason": "no token"}
        )
        headers = {"X-Atlas-Operator-Token": "op-secret"}
        created = c.post(
            f"{BASE}/breakout/versions",
            json={"parameters": {"channel_length": 55}, "reason": "longer range"},
            headers=headers,
        )
        repeat = c.post(
            f"{BASE}/breakout/versions",
            json={"parameters": {"channel_length": 55}, "reason": "same again"},
            headers=headers,
        )
        invalid = c.post(
            f"{BASE}/breakout/versions",
            json={"parameters": {"channel_length": 2}, "reason": "too short"},
            headers=headers,
        )
        enable = c.post(
            f"{BASE}/breakout/status", json={"enabled": True, "reason": "try"}, headers=headers
        )
        fixture = c.post(
            f"{BASE}/fixture_proposer/status",
            json={"enabled": False, "reason": "pause"},
            headers=headers,
        )
    assert unauthorized.status_code == 401
    assert created.status_code == 200
    assert created.json()["created"] is True
    assert created.json()["version"]["version"] == 2
    assert created.json()["version"]["parameters"]["channel_length"] == 55
    assert repeat.json()["created"] is False
    assert invalid.status_code == 422
    assert "greater than or equal to 5" in invalid.json()["error"]["message"]
    assert enable.status_code == 409
    assert fixture.json()["enabled"] is False
    assert fixture.json()["changed"] is True


def test_evaluations_and_proposals_are_served_with_traceability(client, evaluated):
    _, ok, failed = evaluated
    with client() as c:
        evaluations = c.get(f"{BASE}/evaluations", params={"symbol": "FXA"}).json()
        detail = c.get(f"{BASE}/evaluations/{failed.evaluation_id}").json()
        proposals = c.get(f"{BASE}/proposals", params={"status": "ACTIVE"}).json()
        proposal = c.get(f"{BASE}/proposals/{ok.proposal_ids[0]}").json()
        filtered = c.get(f"{BASE}/proposals", params={"strategy_id": "fixture_failing"}).json()
        unknown = c.get(f"{BASE}/proposals", params={"symbol": "NOPE"})
    assert {e["status"] for e in evaluations["evaluations"]} == {"PROPOSED", "FAILED"}
    assert detail["errors"][0]["stage"] == "STRATEGY"
    assert detail["snapshots"][0]["valid_count"] == 2
    [item] = proposals["proposals"]
    assert item["symbol"] == "FXA"
    assert item["direction"] == "LONG"
    assert item["strength_kind"] == "UNCALIBRATED_SCORE"
    assert item["status"] == "ACTIVE"
    assert proposal["snapshots"][0]["id"] == item["feature_snapshot_ids"][0]
    assert "not approved trades" in proposal["notes"][0]
    assert filtered["proposals"] == []
    assert unknown.status_code == 404


def test_proposal_status_follows_the_clock(client, evaluated, lab):
    with client() as c:
        lab.clock.advance(timedelta(days=3))
        expired = c.get(f"{BASE}/proposals", params={"status": "EXPIRED"}).json()
        active = c.get(f"{BASE}/proposals", params={"status": "ACTIVE"}).json()
    assert len(expired["proposals"]) == 1
    assert active["proposals"] == []


def test_feature_registry_endpoint(client):
    with client() as c:
        body = c.get(f"{BASE}/features").json()
    names = {d["name"] for d in body["definitions"]}
    assert {"ema", "sma", "rsi", "atr", "adx", "macd_line", "bollinger_upper"} <= names
    assert {"volume_ratio", "return", "news_event_count", "regime_label"} <= names
    rsi = next(d for d in body["definitions"] if d["name"] == "rsi")
    assert rsi["window_rule"] == "5 * length + 1"
    assert rsi["registered_at"] is not None


def test_custom_registry_context_is_used(lab, make_settings):
    only = StrategyRegistry([ProposingStrategy()])
    ctx = StrategyContext(lab.engine.settings, only, lab.engine, lab.factory, lab.clock)
    app = create_app(make_settings(), clock=lab.clock, health_checks={}, strategies=ctx)
    with TestClient(app) as c:
        ids = [s["strategy_id"] for s in c.get(BASE).json()["strategies"]]
    assert ids == ["fixture_proposer"]

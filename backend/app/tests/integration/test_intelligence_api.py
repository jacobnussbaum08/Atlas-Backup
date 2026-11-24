"""Market intelligence API against real PostgreSQL (Phase 3B).

Data are the MOCK bars and TEST FIXTURE instruments of ``test_market_intelligence``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.runtime import MarketDataContext
from app.main import create_app
from app.market_intelligence.enums import RunKind
from app.market_intelligence.runtime import IntelligenceContext
from app.tests.integration.test_market_intelligence import (
    MEMBERS,
    NOW,
    env,  # noqa: F401  # fixture
    populated,
    service,
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/intelligence"


@pytest.fixture
def client(env, make_settings):  # noqa: F811  # the imported fixture
    def build(now=NOW, **settings) -> TestClient:
        clock = FixedClock(now)
        provider = env.service._provider
        market = MarketDataSettings(_env_file=None)
        svc = service(env, clock)
        app = create_app(
            make_settings(**settings),
            clock=clock,
            health_checks={},
            market_data=MarketDataContext(
                settings=market,
                provider=provider,
                service=HistoricalIngestionService(env.factory, provider, market, clock),
                session_factory=env.factory,
            ),
            market_intelligence=IntelligenceContext(
                svc.settings, provider, svc, env.factory, clock
            ),
        )
        return TestClient(app)

    return build


def run_live(env, at=NOW):  # noqa: F811
    return asyncio.run(service(env, FixedClock(at)).run(RunKind.LIVE, requested_by="test"))


def test_market_conditions(env, client):  # noqa: F811
    before = client().get(f"{BASE}/market").json()
    assert before["available"] is False
    populated(env)
    run = run_live(env)
    body = client(now=NOW + timedelta(minutes=1)).get(f"{BASE}/market").json()
    assert body["available"] is True
    assert body["run"]["id"] == str(run.id)
    assert body["benchmark_symbol"] == "SPY"
    found = {(c["timeframe"], c["dimension"]) for c in body["classifications"]}
    assert found == {
        ("1Day", "TREND"),
        ("1Day", "VOLATILITY"),
        ("1Day", "STABILITY"),
        ("1Week", "TREND"),
        ("1Week", "VOLATILITY"),
    }
    trend = next(c for c in body["classifications"] if c["dimension"] == "TREND")
    assert set(trend["probabilities"]) == {
        "BULLISH",
        "BEARISH",
        "SIDEWAYS",
        "TRANSITIONING",
        "UNCERTAIN",
    }
    assert body["participation"]["status"] == "OK"
    assert body["participation"]["members_evaluated"] == len(MEMBERS)
    assert any("trading signal" in n for n in body["notes"])


def test_point_in_time_view(env, client):  # noqa: F811
    populated(env)
    run_live(env)
    # Asked about a moment before the run finished: nothing was known yet.
    earlier = client().get(
        f"{BASE}/market", params={"at": (NOW - timedelta(minutes=1)).isoformat()}
    )
    assert earlier.json()["available"] is False


def test_asset_measurements_and_relative_strength_ranking(env, client):  # noqa: F811
    populated(env)
    run_live(env)
    body = client().get(f"{BASE}/assets", params={"limit": 50}).json()
    assert body["sort"] == "relative_strength"
    strengths = [i["features"]["relative_strength"] for i in body["items"]]
    present = [float(s) for s in strengths if s is not None]
    assert present == sorted(present, reverse=True)
    assert strengths.index(None) >= len(present) if None in strengths else True
    first = body["items"][0]
    assert first["trend"]["label"] in (
        "BULLISH",
        "BEARISH",
        "SIDEWAYS",
        "TRANSITIONING",
        "UNCERTAIN",
    )
    assert first["volatility"]["dimension"] == "VOLATILITY"
    weekly = client().get(f"{BASE}/assets", params={"timeframe": "1Week"}).json()
    assert {i["timeframe"] for i in weekly["items"]} == {"1Week"}

    detail = client().get(f"{BASE}/assets/{first['asset_id']}").json()
    assert {f["timeframe"] for f in detail["timeframes"]} == {"1Day", "1Week"}
    assert detail["benchmark_relation"]["status"] == "OK"
    assert client().get(f"{BASE}/assets/00000000-0000-0000-0000-000000000000").status_code == 404


def test_correlation_matrix(env, client):  # noqa: F811
    populated(env)
    run_live(env)
    body = client().get(f"{BASE}/correlations").json()
    assert body["window"] == 60
    assert len(body["symbols"]) == len(MEMBERS)
    assert len(body["cells"]) == len(MEMBERS) * (len(MEMBERS) - 1) // 2
    cell = body["cells"][0]
    assert cell["status"] == "OK"
    assert -1 <= float(cell["correlation"]) <= 1
    assert float(cell["ci_low"]) <= float(cell["correlation"]) <= float(cell["ci_high"])
    assert len(body["benchmark"]) == len(MEMBERS)
    assert body["summary"]["absorption_ratio"] is not None


def test_regime_history_distinguishes_recorded_and_reconstructed(env, client):  # noqa: F811
    populated(env, known=NOW - timedelta(hours=3))
    asyncio.run(
        service(env, FixedClock(NOW)).run(
            RunKind.RECONSTRUCTION, requested_by="t", params={"sessions": 4}
        )
    )
    run_live(env, NOW + timedelta(minutes=5))
    api = client(now=NOW + timedelta(minutes=10))
    points = api.get(f"{BASE}/regimes/history").json()["points"]
    bases = [p["basis"] for p in points]
    assert bases.count("RECONSTRUCTED") == 4
    assert bases.count("RECORDED") == 1
    assert all(p["trend"] is not None and p["volatility"] is not None for p in points)
    only_recorded = api.get(f"{BASE}/regimes/history", params={"basis": "RECORDED"}).json()
    assert [p["basis"] for p in only_recorded["points"]] == ["RECORDED"]


def test_starting_runs(env, client, monkeypatch):  # noqa: F811
    populated(env)
    api = client()
    accepted = api.post(f"{BASE}/runs", json={"kind": "LIVE"})
    assert accepted.status_code == 202
    run_id = accepted.json()["id"]
    finished = api.get(f"{BASE}/runs/{run_id}").json()  # the background task has run
    assert finished["status"] == "COMPLETED", finished["error"]
    assert api.post(f"{BASE}/runs", json={"kind": "LIVE", "sessions": 3}).status_code == 422
    # A run still RUNNING blocks another one.
    ctx_service = api.app.state.market_intelligence.service
    ctx_service.create_run(RunKind.LIVE, requested_by="other")
    conflict = api.post(f"{BASE}/runs", json={"kind": "LIVE"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "intelligence_run_in_progress"
    assert [r["requested_by"] for r in api.get(f"{BASE}/runs").json()][:2] == ["other", "api"]


def test_operator_token_is_required_outside_development(env, client):  # noqa: F811
    api = client(
        environment="paper",
        operator_token="op-token-for-tests",
        postgres_password="Unique-Pw-123",
    )
    assert api.post(f"{BASE}/runs", json={"kind": "LIVE"}).status_code == 401


def test_configuration_is_versioned(client):
    body = client().get(f"{BASE}/config").json()
    assert body["code"] == "market-intelligence-default"
    assert body["version"] == 1
    assert [t["timeframe"] for t in body["config"]["timeframes"]] == ["1Day", "1Week"]
    assert body["settings"]["price_adjustment"] == "SPLIT"

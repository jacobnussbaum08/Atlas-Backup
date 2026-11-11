"""Phase 8A: the backtester and the live system decide alike; news availability; the API.

TEST FIXTURE data only (synthetic bars, fixture news pages, 4B fixture strategies).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.backtesting.runtime import BacktestContext
from app.backtesting.service import BacktestService
from app.core.clocks import FixedClock
from app.core.config import Settings
from app.database.models import BacktestProposalRecord, StrategyProposalRecord
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, FeatureRequest, FeatureStatus
from app.feature_engine.stored import StoredContext, compute_stored_features
from app.main import create_app
from app.market_intelligence.enums import KnowledgeBasis
from app.news.enums import NewsJobKind
from app.strategies.enums import EvaluationMode, TriggerKind
from app.tests.fixtures.alpaca_news import news_item, news_page
from app.tests.integration.backtest_support import (
    WallClock,
    make_traded_asset,
    rows,
    seed_series,
    trend,
)
from app.tests.integration.news_support import NOW, make_news_env
from app.tests.integration.strategy_support import ProposingStrategy, evaluation_request, make_lab

pytestmark = pytest.mark.integration


def test_backtest_decisions_equal_the_live_engine_for_the_same_inputs(
    fresh_engine: Engine,
) -> None:
    """The 4B StrategyEngine (HISTORICAL, RECONSTRUCTED, stored proposals) and the replay
    call the same decision step on the same point-in-time inputs: identical proposals."""
    lab = make_lab(fresh_engine, (ProposingStrategy(),), now=datetime.now(UTC))
    lab.clock = WallClock()  # type: ignore[assignment]
    lab.engine.clock = WallClock()
    asset = make_traded_asset(lab.factory, "FIXA")
    seed_series(lab.factory, asset, lambda i, _d: trend(i))
    service = BacktestService(
        lab.factory, WallClock(), registry=lab.registry, version=lambda: "test-code"
    )
    from app.backtesting.config import BacktestRequest

    run_id = service.create(
        BacktestRequest.model_validate(
            {
                "name": "parity",
                "start_date": "2026-10-05",
                "end_date": "2026-10-09",
                "universe": {"mode": "EXPLICIT", "symbols": ["FIXA"]},
                "strategies": [{"strategy_id": "fixture_proposer"}],
                "benchmark_symbol": None,
            }
        ),
        actor="operator:test",
    )
    service.execute(run_id)
    with session_scope(lab.factory) as session:
        replay = session.scalars(
            select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == run_id)
        ).all()
        assert replay
        for p in replay:
            result = lab.engine.evaluate(
                evaluation_request(
                    lab,
                    asset,
                    mode=EvaluationMode.HISTORICAL,
                    basis=KnowledgeBasis.RECONSTRUCTED,
                    data_cutoff=p.decided_at,
                    trigger=TriggerKind.BAR_CLOSE,
                    trigger_ref=f"1Day:{p.session.isoformat()}",
                    timeframe=p.timeframe,
                )
            )
            assert result.outcome == "EVALUATED", result
            live = session.scalars(
                select(StrategyProposalRecord).where(
                    StrategyProposalRecord.id.in_(result.proposal_ids)
                )
            ).all()
            assert [
                (
                    x.direction.value,
                    x.reference_entry,
                    x.invalidation_level,
                    x.target_reference,
                    x.signal_strength,
                    x.expires_at,
                )
                for x in live
            ] == [
                (
                    p.direction,
                    p.reference_entry,
                    p.invalidation_level,
                    p.target_reference,
                    p.signal_strength,
                    p.expires_at,
                )
            ]


# ---------------------------------------------------------------------- news
def _news_count(env: Any, cutoff: datetime, delay: timedelta) -> Any:
    feature = REGISTRY.resolve(FeatureRequest("news_event_count", "1Day", {"lookback_hours": 24}))
    ctx = StoredContext(
        env.assets["FXC"],
        cutoff,
        NOW + timedelta(hours=1),
        KnowledgeBasis.RECONSTRUCTED,
        news_delay=delay,
    )
    with session_scope(env.factory) as session:
        return compute_stored_features(session, [feature], ctx)[feature.key]


def test_historical_news_counts_only_after_the_conservative_delay(fresh_engine: Engine) -> None:
    env = make_news_env(fresh_engine)
    env.fake.add(
        news_page(
            [
                news_item(1, NOW - timedelta(hours=3), symbols=["FXC"]),
                news_item(2, NOW - timedelta(minutes=62), symbols=["FXC"]),
            ]
        )
    )
    job = env.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=NOW - timedelta(hours=48),
        end=NOW,
        requested_by="test",
    )
    asyncio.run(env.service.run_job(job.id))
    cutoff = NOW - timedelta(hours=1)  # article 2 was published 2 minutes before the cutoff
    immediate = _news_count(env, cutoff, timedelta(0))
    delayed = _news_count(env, cutoff, timedelta(minutes=5))
    assert (immediate.status, immediate.value) == (FeatureStatus.VALID, 2.0)
    # Backfilled news has no recorded availability: with the backtest's 5-minute delay an
    # article published 2 minutes before the decision does not count.
    assert (delayed.status, delayed.value) == (FeatureStatus.VALID, 1.0)
    future = _news_count(env, NOW - timedelta(hours=4), timedelta(minutes=5))
    assert future.value == 0.0  # nothing published after the cutoff ever counts


# ---------------------------------------------------------------------- API
@pytest.fixture
def api(fresh_engine: Engine, make_settings: Any) -> Any:
    lab = make_lab(fresh_engine, (ProposingStrategy(),), now=datetime.now(UTC))
    asset = make_traded_asset(lab.factory, "FIXA")
    seed_series(lab.factory, asset, lambda i, _d: trend(i))
    service = BacktestService(
        lab.factory, WallClock(), registry=lab.registry, version=lambda: "test-code"
    )
    context = BacktestContext(lab.factory, WallClock(), lab.registry, service)
    settings: Settings = make_settings(operator_token="op-secret")
    app = create_app(settings, clock=FixedClock(NOW), health_checks={}, backtesting=context)
    return TestClient(app), context


BODY = {
    "name": "api run",
    "start_date": "2026-10-01",
    "end_date": "2026-10-28",
    "universe": {"mode": "EXPLICIT", "symbols": ["FIXA"]},
    "strategies": [{"strategy_id": "fixture_proposer"}],
    "directions": ["LONG"],
    "benchmark_symbol": None,
}


def test_the_api_creates_runs_with_an_operator_token_and_reads_every_result(api: Any) -> None:
    client, context = api
    with client:
        assert client.post("/api/v1/backtests", json=BODY).status_code == 401
        bad = client.post(
            "/api/v1/backtests",
            json={**BODY, "end_date": "2026-01-01"},
            headers={"X-Atlas-Operator-Token": "op-secret"},
        )
        assert bad.status_code == 422
        created = client.post(
            "/api/v1/backtests", json=BODY, headers={"X-Atlas-Operator-Token": "op-secret"}
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        assert created.json()["config"]["strategies"][0]["version"] >= 1
        context.drain()  # wait for the background replay
        detail = client.get(f"/api/v1/backtests/{run_id}").json()
        assert detail["status"] == "COMPLETED", detail
        assert detail["progress"] == 1.0
        for path in (
            "equity",
            "metrics",
            "benchmarks",
            "trades",
            "orders",
            "fills",
            "proposals",
            "rejections",
            "events",
            "errors",
            "evaluations",
            "journal",
            "progress",
        ):
            response = client.get(f"/api/v1/backtests/{run_id}/{path}")
            assert response.status_code == 200, (path, response.text)
        trades = client.get(f"/api/v1/backtests/{run_id}/trades").json()
        assert trades
        one = client.get(f"/api/v1/backtests/{run_id}/trades/{trades[0]['seq']}").json()
        assert one["proposal"] is not None
        assert one["orders"]
        assert one["fills"]
        equity = client.get(f"/api/v1/backtests/{run_id}/equity").json()
        assert len(equity) == 20
        assert Decimal(equity[0]["equity"]) > 0
        listing = client.get("/api/v1/backtests").json()
        assert [r["id"] for r in listing] == [run_id]
        options = client.get("/api/v1/backtests/options").json()
        assert {s["strategy_id"] for s in options["strategies"]} == {"fixture_proposer"}
        assert any(a["symbol"] == "FIXA" for a in options["assets"])
        reproduced = client.post(
            f"/api/v1/backtests/{run_id}/reproduce", headers={"X-Atlas-Operator-Token": "op-secret"}
        )
        assert reproduced.status_code == 202
        context.drain()
        again = client.get(f"/api/v1/backtests/{reproduced.json()['id']}").json()
        assert again["reproduction_matches"] is True
        compare = client.get(
            "/api/v1/backtests/compare", params={"run_ids": [run_id, reproduced.json()["id"]]}
        ).json()
        totals = [r["portfolio"]["total_return"]["value"] for r in compare["runs"]]
        assert totals[0] == totals[1]
        assert client.get(f"/api/v1/backtests/{uuid.uuid4()}").status_code == 404
    _ = (date, rows)

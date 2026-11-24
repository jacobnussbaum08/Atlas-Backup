"""Decision coordinator against real PostgreSQL (Phase 5B). All data are TEST FIXTURES."""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.ai_engine.config import AISettings
from app.ai_engine.coordinator import gather as gather_module
from app.ai_engine.coordinator.baselines import evaluate_decisions
from app.ai_engine.coordinator.policy import DecisionPolicy, Weights
from app.ai_engine.coordinator.service import Coordinator
from app.ai_engine.enums import DecisionMode, DecisionOutcome, DecisionRunStatus, InputKind
from app.ai_engine.runtime import AIContext
from app.asset_discovery.sessions import CALENDARS, trading_day
from app.core.clocks import FixedClock
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.database.models import (
    AIDecisionRecord,
    DecisionInputRecord,
    DecisionPolicyVersionRecord,
    DecisionRunRecord,
    MarketBarRecord,
)
from app.database.sessions import session_scope
from app.main import create_app
from app.market_intelligence.enums import Dimension, KnowledgeBasis
from app.strategies.enums import Direction, StrategyFamily
from app.tests.integration.ai_support import (
    DECISION_AT,
    NOW,
    make_asset,
    make_market_regime,
    make_proposal,
    make_strategy,
)

pytestmark = pytest.mark.integration
CAL = CALENDARS["XNYS"]
POLICY = DecisionPolicy()


@pytest.fixture
def env(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    clock = FixedClock(NOW)
    ids = {
        "AAA": make_asset(factory, "AAA"),
        "BBB": make_asset(factory, "BBB"),
        "momentum": make_strategy(factory, "momentum", StrategyFamily.MOMENTUM),
        "breakout": make_strategy(factory, "breakout", StrategyFamily.BREAKOUT),
        "mean_reversion": make_strategy(factory, "mean_reversion", StrategyFamily.MEAN_REVERSION),
    }
    return factory, clock, Coordinator(factory, clock, worker_id="test"), ids


def proposal(env, symbol, strategy, *, stored_at, **kwargs):
    factory, _, _, ids = env
    return make_proposal(
        factory,
        strategy_id=strategy,
        version_id=ids[strategy],
        asset_id=ids[symbol],
        stored_at=stored_at,
        **kwargs,
    )


def run_at(env, at=DECISION_AT, basis=KnowledgeBasis.RECORDED, policy=POLICY):
    return env[2].run(policy, mode=DecisionMode.HISTORICAL, decision_at=at, basis=basis)


def inputs(env, run_id, kind):
    with session_scope(env[0]) as session:
        return list(
            session.scalars(
                select(DecisionInputRecord).where(
                    DecisionInputRecord.run_id == run_id, DecisionInputRecord.kind == kind
                )
            )
        )


def decisions(env, run_id):
    with session_scope(env[0]) as session:
        return list(
            session.scalars(select(AIDecisionRecord).where(AIDecisionRecord.run_id == run_id))
        )


def test_recorded_runs_see_only_what_was_stored_by_the_decision_time(env):
    early = proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=50))
    late = proposal(env, "BBB", "breakout", stored_at=DECISION_AT + timedelta(hours=3))
    make_market_regime(
        env[0],
        label="BULLISH",
        dimension=Dimension.TREND,
        available_at=DECISION_AT + timedelta(hours=1),
    )
    recorded = run_at(env)
    assert recorded.status is DecisionRunStatus.COMPLETED
    ids = {r.ref for r in inputs(env, recorded.run_id, InputKind.STRATEGY_PROPOSAL)}
    assert ids == {str(early)}
    assert inputs(env, recorded.run_id, InputKind.MARKET_REGIME) == []
    for row in inputs(env, recorded.run_id, InputKind.STRATEGY_PROPOSAL):
        assert row.available_at <= DECISION_AT

    reconstructed = run_at(env, basis=KnowledgeBasis.RECONSTRUCTED)
    ids = {r.ref for r in inputs(env, reconstructed.run_id, InputKind.STRATEGY_PROPOSAL)}
    assert ids == {str(early), str(late)}
    assert len(inputs(env, reconstructed.run_id, InputKind.MARKET_REGIME)) == 1
    with session_scope(env[0]) as session:
        run = session.get(DecisionRunRecord, reconstructed.run_id)
        assert run is not None
        assert run.knowledge_at == NOW
        assert run.basis is KnowledgeBasis.RECONSTRUCTED


def test_expired_and_future_proposals_are_not_considered(env):
    proposal(
        env,
        "AAA",
        "momentum",
        stored_at=DECISION_AT - timedelta(days=3),
        cutoff=DECISION_AT - timedelta(days=3),
        expires_at=DECISION_AT - timedelta(hours=1),
    )
    proposal(
        env,
        "BBB",
        "momentum",
        stored_at=DECISION_AT - timedelta(minutes=1),
        cutoff=DECISION_AT + timedelta(hours=1),
    )
    result = run_at(env)
    assert inputs(env, result.run_id, InputKind.STRATEGY_PROPOSAL) == []
    [only] = decisions(env, result.run_id)
    assert only.outcome is DecisionOutcome.NO_OPPORTUNITY


def test_runs_are_idempotent_and_records_immutable(env):
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))
    first = run_at(env)
    again = run_at(env)
    assert again.run_id == first.run_id
    assert again.created is False
    factory = env[0]
    with pytest.raises(DBAPIError), session_scope(factory) as session:
        session.execute(text("UPDATE ai_decisions SET explanation = 'edited'"))
    with pytest.raises(DBAPIError), session_scope(factory) as session:
        session.execute(text("UPDATE decision_runs SET outcome = 'NO_OPPORTUNITY'"))
    with pytest.raises(DBAPIError), session_scope(factory) as session:
        session.execute(text("DELETE FROM decision_inputs"))


def test_replay_reproduces_a_run_from_stored_inputs(env):
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))
    proposal(env, "AAA", "breakout", stored_at=DECISION_AT - timedelta(minutes=20), entry="100.4")
    proposal(
        env,
        "AAA",
        "mean_reversion",
        direction=Direction.SHORT,
        stored_at=DECISION_AT - timedelta(minutes=10),
        strength="0.4",
    )
    make_market_regime(
        env[0],
        label="BULLISH",
        dimension=Dimension.TREND,
        available_at=DECISION_AT - timedelta(hours=2),
    )
    result = run_at(env)
    # Information arriving later does not change the stored run or its replay.
    proposal(env, "BBB", "momentum", stored_at=DECISION_AT - timedelta(minutes=5))
    replay = env[2].replay(result.run_id)
    assert replay["inputs_identical"] is True
    assert replay["result_identical"] is True
    assert replay["differing_decisions"] == []
    assert "no provider was called" in replay["note"]
    for d in decisions(env, result.run_id):
        assert d.expires_at > d.decision_at >= d.data_cutoff


def test_policy_versions_are_content_addressed(env):
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))
    a = run_at(env)
    b = run_at(env, at=DECISION_AT + timedelta(hours=1))
    c = run_at(env, policy=DecisionPolicy(weights=Weights(news=0.0)))
    with session_scope(env[0]) as session:
        versions = {r.id: r.policy_version_id for r in session.scalars(select(DecisionRunRecord))}
        stored = list(session.scalars(select(DecisionPolicyVersionRecord)))
    assert versions[a.run_id] == versions[b.run_id] != versions[c.run_id]
    assert sorted(v.version for v in stored) == [1, 2]


def test_a_failed_run_is_recorded_and_can_be_retried(env, monkeypatch):
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))

    def boom(*args, **kwargs):
        raise RuntimeError("fixture failure password=hunter2")

    monkeypatch.setattr(gather_module, "gather", boom)
    with pytest.raises(RuntimeError):
        run_at(env)
    with session_scope(env[0]) as session:
        [failed] = list(session.scalars(select(DecisionRunRecord)))
        assert failed.status is DecisionRunStatus.FAILED
        assert failed.error is not None
        assert "hunter2" not in failed.error
    monkeypatch.undo()
    retried = run_at(env)
    assert retried.created
    assert retried.status is DecisionRunStatus.COMPLETED


def _bars(factory, asset_id, opens_closes):
    rows = []
    day = DECISION_AT.astimezone(CAL.zone).date()
    for open_, close in opens_closes:
        day += timedelta(days=1)
        while trading_day(CAL, day) is None:
            day += timedelta(days=1)
        at = datetime.combine(day, time(0), tzinfo=CAL.zone).astimezone(UTC)
        known = at + timedelta(days=1)
        rows.append(
            {
                "asset_id": asset_id,
                "provider": "alpaca",
                "feed": "iex",
                "provider_symbol": "AAA",
                "timeframe": "1Day",
                "adjustment": PriceAdjustment.SPLIT,
                "event_time": at,
                "revision": 0,
                "open": Decimal(open_),
                "high": Decimal(max(open_, close)) + 1,
                "low": Decimal(min(open_, close)) - 1,
                "close": Decimal(close),
                "volume": Decimal(1000),
                "vwap": None,
                "trade_count": 10,
                "is_complete": True,
                "source": DataSource.HISTORICAL_REST,
                "observation_kind": BarObservationKind.HISTORICAL,
                "quality_status": DataQualityStatus.VALID,
                "received_at": known,
                "available_at": known,
                "original_available_at": None,
                "processed_at": known,
                "ingested_at": known,
            }
        )
    with session_scope(factory) as session:
        session.execute(insert(MarketBarRecord), rows)


def test_baselines_use_realized_net_outcomes_and_need_evidence(env):
    factory, _, _, ids = env
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))
    run_at(env)
    with session_scope(factory) as session:
        early = evaluate_decisions(session, calendar=CAL, known_at=NOW, store=False)
    assert early["results"]["outcome_status_counts"] == {"IMMATURE": 1}
    assert early["verdict"] == "INSUFFICIENT_EVIDENCE"
    _bars(factory, ids["AAA"], [(100, 101), (101, 102), (102, 103), (103, 104), (104, 105)])
    with session_scope(factory) as session:
        later = evaluate_decisions(
            session, calendar=CAL, known_at=NOW + timedelta(days=10), cost_bps=10.0
        )
    take_all = later["results"]["take_all"]
    assert take_all["observations"] == 1
    # Entry at the next open (100), exit at the fifth close (105), minus 10 bps.
    assert take_all["mean"] == pytest.approx(0.05 - 0.001)
    assert later["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert later["id"] is not None


def test_api_exposes_decisions_without_any_write_endpoint(env, make_settings):
    factory, clock, coordinator, _ = env
    proposal(env, "AAA", "momentum", stored_at=DECISION_AT - timedelta(minutes=30))
    proposal(
        env,
        "AAA",
        "mean_reversion",
        direction=Direction.SHORT,
        stored_at=DECISION_AT - timedelta(minutes=20),
    )
    result = run_at(env)
    ctx = AIContext(
        settings=AISettings(_env_file=None),
        coordinator=coordinator,
        policy=POLICY,
        session_factory=factory,
        clock=clock,
        calendar=CAL,
    )
    app = create_app(make_settings(), clock=clock, health_checks={}, ai=ctx)
    with TestClient(app) as client:
        overview = client.get("/api/v1/ai/overview").json()
        assert overview["runs"] == {"COMPLETED": 1}
        assert "not an approved trade" in overview["disclaimer"]
        detail = client.get(f"/api/v1/ai/runs/{result.run_id}").json()
        assert detail["input_counts"]["STRATEGY_PROPOSAL"] == 2
        assert any(c["type"] == "OPPOSING_DIRECTIONS" for c in detail["conflicts"])
        ranked = client.get("/api/v1/ai/rankings").json()
        assert ranked["run"]["id"] == str(result.run_id)
        decision_id = detail["decisions"][0]["id"]
        found = client.get(f"/api/v1/ai/decisions/{decision_id}").json()
        assert found["components"]
        assert found["proposals"]
        assert "not probabilities" in found["scores"]["note"]
        assert client.get(f"/api/v1/ai/decisions/{decision_id}/evidence").status_code == 200
        assert (
            client.get(f"/api/v1/ai/decisions/{decision_id}/models").json()["model_versions"] == []
        )
        assert client.get(f"/api/v1/ai/runs/{result.run_id}/replay").json()["result_identical"]
        assert client.get("/api/v1/ai/decisions", params={"outcome": "DEFER"}).status_code == 200
        assert len(client.get("/api/v1/ai/policies").json()) == 1
        usage = client.get("/api/v1/ai/llm/usage").json()
        assert usage["configuration"]["provider"] == "none"
        assert "api_key" not in str(usage).lower()
        assert client.get(f"/api/v1/ai/decisions/{uuid.uuid4()}").status_code == 404
        assert client.post("/api/v1/ai/runs").status_code == 405


def test_coordinator_has_no_path_to_execution_risk_or_llm_calls():
    root = Path(__file__).resolve().parents[2] / "ai_engine"
    forbidden = ("app.execution", "app.risk", "app.portfolio", "app.paper_broker", "app.accounting")
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        assert not [n for n in names if n.startswith(forbidden)], path
        if path.parent.name == "coordinator":
            # Decisions read stored LLM analyses; they never call a provider.
            assert not [n for n in names if n.startswith("app.ai_engine.llm")], path
            assert "httpx" not in names, path

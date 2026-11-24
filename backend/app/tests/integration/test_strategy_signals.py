"""The six Phase 4B strategies through the engine, against real PostgreSQL.

Bars are MOCK observations built from DETERMINISTIC FIXTURE series
(``tests/fixtures/strategy_series``); assets and their provider reference data are TEST
FIXTURES. Nothing here is market data, and nothing measures performance: the tests check
persistence, signal states, eligibility, duplicate prevention, conflicts, expiry and
point-in-time correctness.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database.models import (
    ActorType,
    AssetClass,
    StrategyConfigurationRecord,
    StrategyEvaluationRecord,
    StrategyEvaluationSnapshotRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalConflictRecord,
    StrategyProposalRecord,
    StrategyRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies import catalog, queries
from app.strategies.enums import (
    ConflictKind,
    Direction,
    EvaluationMode,
    EvaluationStatus,
    ProposalStatus,
    SignalState,
    TriggerKind,
)
from app.strategies.registry import discover
from app.tests.fixtures.strategy_series import (
    benchmark_flat,
    compression_expansion,
    dip_in_uptrend,
    event_reaction,
    from_closes,
    mirror,
    momentum_up,
    range_breakout,
    ranging,
    trend_up,
)
from app.tests.integration.strategy_support import (
    NOW,
    AlwaysShortStrategy,
    FailingStrategy,
    evaluation_request,
    make_asset,
    make_lab,
    seed_candles,
)

pytestmark = pytest.mark.integration

SCENARIOS = {
    "momentum": momentum_up,
    "trend_following": trend_up,
    "mean_reversion": dip_in_uptrend,
    "breakout": range_breakout,
    "volatility": compression_expansion,
}
EVENT = "BAR_CLOSE:1Day:2026-11-03"


@pytest.fixture
def lab(fresh_engine):
    return make_lab(fresh_engine, [*discover(), AlwaysShortStrategy(), FailingStrategy()])


def setup_asset(lab, sid, symbol="FXA", candles=None, **asset_kwargs):
    candles = candles if candles is not None else SCENARIOS[sid]()
    asset = make_asset(lab.factory, symbol, **asset_kwargs)
    seed_candles(lab.factory, asset, candles)
    if sid == "momentum":
        spy = make_asset(lab.factory, "SPY", AssetClass.ETF)
        seed_candles(lab.factory, spy, benchmark_flat(len(candles)))
    return asset


def evaluation(lab, evaluation_id) -> StrategyEvaluationRecord:
    with session_scope(lab.factory) as session:
        record = session.get(StrategyEvaluationRecord, evaluation_id)
        assert record is not None
        return record


def proposals(lab) -> list[StrategyProposalRecord]:
    with session_scope(lab.factory) as session:
        return list(session.scalars(select(StrategyProposalRecord)))


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_strategy_records_a_traceable_proposal(lab, sid):
    asset = setup_asset(lab, sid)
    result = lab.engine.evaluate(evaluation_request(lab, asset, sid))
    assert (result.status, result.signal_state) == (EvaluationStatus.PROPOSED, SignalState.SIGNAL)
    record = evaluation(lab, result.evaluation_id)
    assert record.signal_state is SignalState.SIGNAL
    assert record.signal_reason is None
    assert record.checks
    assert all(
        {"name", "passed", "detail", "direction", "features"} <= set(c) for c in record.checks
    )
    assert record.eligibility is not None
    assert record.eligibility["long_eligible"] is True
    [proposal] = proposals(lab)
    assert proposal.direction is Direction.LONG
    assert proposal.evaluation_event == EVENT
    assert proposal.invalidation_level is not None
    assert proposal.target_reference is not None
    assert proposal.invalidation_level < proposal.reference_entry < proposal.target_reference
    assert proposal.levels["entry_method"] == "newest completed close"
    assert proposal.levels["risk_per_unit"]
    with session_scope(lab.factory) as session:
        version = catalog.latest_version(session, sid)
        snapshot = session.get(StrategyFeatureSnapshotRecord, proposal.feature_snapshot_ids[0])
    assert version is not None
    assert version.logic_version.endswith("-1")
    assert version.implemented is True
    assert snapshot is not None
    assert snapshot.inputs_available_at is not None
    assert snapshot.inputs_available_at <= record.data_cutoff
    for item in proposal.evidence:
        assert item["feature_key"] in snapshot.features
        assert item["available_at"] <= record.data_cutoff.isoformat()
    if sid == "momentum":
        relative = next(k for k in snapshot.features if k.startswith("relative_return.v1("))
        assert snapshot.features[relative]["status"] == "VALID"
        assert snapshot.features[relative]["inputs"]["benchmark"] == "SPY"


def test_signal_states_distinguish_why_nothing_was_proposed(lab):
    few = setup_asset(lab, "trend_following", "FEW", candles=trend_up()[-30:])
    gap = make_asset(lab.factory, "GAP")
    candles = trend_up()
    seed_candles(lab.factory, gap, candles, skip=[len(candles) - 3])
    flat = setup_asset(
        lab, "trend_following", "FLT", candles=from_closes(ranging(320, 60.0, 0.02, 0.02, 21))
    )
    news = setup_asset(lab, "event_driven", "NWS", candles=event_reaction())
    unknown = setup_asset(lab, "trend_following", "UNK", reference=False)
    untradable = setup_asset(lab, "trend_following", "UNT", tradable=False)
    late = setup_asset(lab, "trend_following", "LTR", observed_at=NOW + timedelta(hours=1))

    def state(asset, sid="trend_following"):
        result = lab.engine.evaluate(evaluation_request(lab, asset, sid))
        return result.status, result.signal_state, evaluation(lab, result.evaluation_id)

    assert state(few)[:2] == (EvaluationStatus.SKIPPED, SignalState.INSUFFICIENT_DATA)
    assert state(gap)[:2] == (EvaluationStatus.SKIPPED, SignalState.MISSING_DATA)
    status, signal, record = state(flat)
    assert (status, signal) == (EvaluationStatus.NO_PROPOSAL, SignalState.NO_SIGNAL)
    assert record.signal_reason
    assert "failed" in record.signal_reason
    assert any(c["passed"] is False for c in record.checks)
    # Missing news coverage is unavailable data, never "no news".
    assert state(news, "event_driven")[:2] == (
        EvaluationStatus.SKIPPED,
        SignalState.UNAVAILABLE_DATA,
    )
    for asset in (unknown, untradable, late):
        status, signal, record = state(asset)
        assert (status, signal) == (EvaluationStatus.SKIPPED, SignalState.INELIGIBLE_ASSET)
        with session_scope(lab.factory) as session:
            linked = session.scalar(
                select(func.count()).where(
                    StrategyEvaluationSnapshotRecord.evaluation_id == record.id
                )
            )
        assert linked == 0  # rejected before any input was computed
    reasons = {
        "UNK2": setup_asset(lab, "trend_following", "UNK2", reference=False),
        "UNT2": setup_asset(lab, "trend_following", "UNT2", tradable=False),
    }
    assert "reference data" in (state(reasons["UNK2"])[2].signal_reason or "")
    assert "not tradable" in (state(reasons["UNT2"])[2].signal_reason or "")
    failing = lab.engine.evaluate(evaluation_request(lab, few, "fixture_failing"))
    assert failing.signal_state is SignalState.FAILED
    assert evaluation(lab, failing.evaluation_id).signal_state is SignalState.FAILED
    assert proposals(lab) == []


def test_short_proposals_need_a_shortable_asset(lab):
    candles = mirror(trend_up())
    shortable = setup_asset(lab, "trend_following", "SHT", candles=candles)
    hard = setup_asset(lab, "trend_following", "HTB", candles=candles, easy_to_borrow=False)
    ok = lab.engine.evaluate(evaluation_request(lab, shortable, "trend_following"))
    blocked = lab.engine.evaluate(evaluation_request(lab, hard, "trend_following"))
    assert ok.signal_state is SignalState.SIGNAL
    assert [p.direction for p in proposals(lab)] == [Direction.SHORT]
    assert blocked.signal_state is SignalState.NO_SIGNAL
    record = evaluation(lab, blocked.evaluation_id)
    assert record.eligibility is not None
    assert record.eligibility["short_eligible"] is False
    assert any(c["name"] == "eligibility" and c["passed"] is False for c in record.checks)
    # Configured for SHORT only, the asset is not eligible at all.
    with session_scope(lab.factory) as session:
        catalog.create_version(
            session,
            lab.registry.get("trend_following"),
            {"directions": ["SHORT"]},
            reason="short only",
            actor="test",
        )
    short_only = lab.engine.evaluate(
        evaluation_request(lab, hard, "trend_following", trigger_ref="1Day:short-only")
    )
    assert short_only.signal_state is SignalState.INELIGIBLE_ASSET


def test_the_same_event_is_never_proposed_twice(lab):
    asset = setup_asset(lab, "trend_following")
    first = lab.engine.evaluate(evaluation_request(lab, asset, "trend_following"))
    assert first.signal_state is SignalState.SIGNAL
    with session_scope(lab.factory) as session:
        catalog.create_version(
            session,
            lab.registry.get("trend_following"),
            {"signal_ttl_periods": 2},
            reason="longer expiry",
            actor="test",
        )
    # A new version re-evaluates the same completed period (a different idempotency key).
    again = lab.engine.evaluate(evaluation_request(lab, asset, "trend_following"))
    assert again.outcome == "EVALUATED"
    assert (again.status, again.signal_state) == (
        EvaluationStatus.NO_PROPOSAL,
        SignalState.DUPLICATE,
    )
    record = evaluation(lab, again.evaluation_id)
    assert any(str(first.proposal_ids[0]) in note for note in record.notes)
    assert len(proposals(lab)) == 1


def test_opposing_proposals_are_recorded_as_conflicts_not_resolved(lab):
    asset = setup_asset(lab, "trend_following")
    long = lab.engine.evaluate(evaluation_request(lab, asset, "trend_following"))
    short = lab.engine.evaluate(evaluation_request(lab, asset, "fixture_short"))
    assert long.signal_state is short.signal_state is SignalState.SIGNAL
    with session_scope(lab.factory) as session:
        [conflict] = session.scalars(select(StrategyProposalConflictRecord)).all()
    pair = {long.proposal_ids[0], short.proposal_ids[0]}
    assert {conflict.proposal_a_id, conflict.proposal_b_id} == pair
    assert conflict.proposal_a_id < conflict.proposal_b_id
    assert conflict.kind is ConflictKind.OPPOSING_DIRECTION
    assert conflict.detected_by == short.proposal_ids[0]
    assert conflict.overlap_start < conflict.overlap_end
    # Both proposals stay active: nothing was decided.
    with session_scope(lab.factory) as session:
        views = queries.proposals(session, NOW)
        listed = queries.conflicts(session, NOW, asset_id=asset)
    assert {v.status for v in views} == {ProposalStatus.ACTIVE}
    assert {v.conflict_count for v in views} == {1}
    assert listed[0].same_strategy is False
    assert {p.direction for p in listed[0].proposals} == {Direction.LONG, Direction.SHORT}
    # A historical re-evaluation does not conflict with live proposals.
    lab.engine.evaluate(
        evaluation_request(
            lab,
            asset,
            "fixture_short",
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECORDED,
            data_cutoff=NOW - timedelta(minutes=5),
            trigger=TriggerKind.MANUAL,
            trigger_ref="cli:history",
        )
    )
    with session_scope(lab.factory) as session:
        total = session.scalar(select(func.count()).select_from(StrategyProposalConflictRecord))
    assert total == 1


def test_one_failing_strategy_does_not_interrupt_the_six(lab):
    candles = trend_up()
    asset = setup_asset(lab, "momentum", candles=candles)
    requests = [evaluation_request(lab, asset, s.strategy_id) for s in discover()]
    requests.append(evaluation_request(lab, asset, "fixture_failing"))
    results = asyncio.run(lab.engine.evaluate_many(requests))
    by_id = {r.request.strategy_id: r for r in results}
    assert by_id["fixture_failing"].signal_state is SignalState.FAILED
    for s in discover():
        assert by_id[s.strategy_id].outcome == "EVALUATED"
        assert by_id[s.strategy_id].signal_state is not SignalState.FAILED


def test_the_period_in_progress_is_never_a_breakout_input(lab):
    asset = setup_asset(lab, "breakout")  # the breakout candle is the 3 Nov session
    intraday = lab.engine.evaluate(
        evaluation_request(
            lab,
            asset,
            "breakout",
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECONSTRUCTED,
            data_cutoff=NOW.replace(hour=20, minute=0),  # 3 Nov session still open
            trigger=TriggerKind.MANUAL,
            trigger_ref="intraday",
        )
    )
    assert intraday.signal_state is SignalState.NO_SIGNAL
    with session_scope(lab.factory) as session:
        snapshot_id = session.scalar(
            select(StrategyEvaluationSnapshotRecord.snapshot_id).where(
                StrategyEvaluationSnapshotRecord.evaluation_id == intraday.evaluation_id
            )
        )
        snapshot = session.get(StrategyFeatureSnapshotRecord, snapshot_id)
    assert snapshot is not None
    assert snapshot.last_period_event_at is not None
    assert snapshot.last_period_event_at < NOW.replace(hour=5)  # 2 Nov, not 3 Nov
    after_close = lab.engine.evaluate(evaluation_request(lab, asset, "breakout"))
    assert after_close.signal_state is SignalState.SIGNAL


def test_historical_proposals_use_their_cutoff_and_expire(lab):
    asset = setup_asset(lab, "volatility")
    cutoff = NOW - timedelta(minutes=10)
    result = lab.engine.evaluate(
        evaluation_request(
            lab,
            asset,
            "volatility",
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECORDED,
            data_cutoff=cutoff,
            trigger=TriggerKind.MANUAL,
            trigger_ref=f"cli:{cutoff.isoformat()}",
        )
    )
    assert result.signal_state is SignalState.SIGNAL
    [proposal] = proposals(lab)
    assert proposal.signal_at == proposal.data_cutoff == cutoff
    assert all(e["available_at"] <= cutoff.isoformat() for e in proposal.evidence)
    lab.clock.set(proposal.expires_at + timedelta(minutes=1))
    assert lab.engine.expire_due() == 1
    with session_scope(lab.factory) as session:
        view = queries.proposal(session, proposal.id, lab.clock.now())
    assert view is not None
    assert view.status is ProposalStatus.EXPIRED


OLD_MOMENTUM_FIELDS = (
    "timeframes",
    "directions",
    "signal_ttl_periods",
    "holding_horizon_periods",
    "min_signal_strength",
    "lookback_periods",
    "acceleration_periods",
    "persistence_periods",
    "rsi_length",
    "trend_ema_length",
    "volume_short",
    "volume_long",
    "min_volume_ratio",
)


def test_sync_upgrades_an_interface_version_keeping_its_stored_parameters(fresh_engine):
    """A Phase 4A interface version (fewer parameter fields) syncs to a Phase 4B version
    that keeps every stored value and writes the new fields' defaults out."""
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    strategy = next(s for s in discover() if s.strategy_id == "momentum")
    full = catalog.parameters_json(strategy.parse_parameters({"lookback_periods": 30}))
    old = {k: v for k, v in full.items() if k in OLD_MOMENTUM_FIELDS}
    with session_scope(factory) as session:
        session.add(StrategyRecord(id="momentum", family=strategy.family))
        session.flush()
        config = StrategyConfigurationRecord(
            strategy_id="momentum",
            parameters=old,
            parameters_sha256=sha256_json(old),
            schema_sha256="phase-4a",
            created_by="phase-4a",
        )
        session.add(config)
        session.flush()
        metadata = catalog._metadata(strategy, strategy.parse_parameters(old), REGISTRY)
        metadata |= {"logic_version": "momentum-interface-1", "implemented": False}
        catalog._add_version(
            session,
            strategy,
            config,
            metadata,
            reason="Phase 4A interface",
            actor="test",
            actor_type=ActorType.SYSTEM,
            previous=None,
        )
    with session_scope(factory) as session:
        report = catalog.sync_strategies(session, [strategy])
        version = catalog.latest_version(session, "momentum")
        assert version is not None
        params = catalog.configuration(session, version).parameters
    assert report.problems == []
    assert report.versions_created == ["momentum v2"]
    assert version.logic_version == "momentum-1"
    assert version.implemented is True
    assert "long_lookback_periods" in version.reason
    assert params["lookback_periods"] == 30  # the stored value is kept
    assert params["long_lookback_periods"] == 60  # the new field's default is explicit

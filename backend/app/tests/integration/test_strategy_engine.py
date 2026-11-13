"""Strategy evaluation engine against real PostgreSQL (Phase 4A).

Bars are MOCK observations and strategies are TEST FIXTURE strategies
(``strategy_support``); nothing here is market data or trading logic.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.database.models import (
    AssetClass,
    StrategyEvaluationErrorRecord,
    StrategyEvaluationRecord,
    StrategyEvaluationSnapshotRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
)
from app.database.sessions import session_scope
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies import queries
from app.strategies.engine import Claim, MarketUpdate
from app.strategies.enums import (
    Direction,
    ErrorStage,
    EvaluationMode,
    EvaluationStatus,
    ProposalEventKind,
    ProposalStatus,
    StrengthKind,
    TriggerKind,
)
from app.tests.integration.strategy_support import (
    NOW,
    FailingStrategy,
    NewsFixtureStrategy,
    ProposingStrategy,
    SlowStrategy,
    UntraceableStrategy,
    bar_close,
    evaluation_request,
    make_asset,
    make_lab,
    seed_bars,
    sessions,
)

pytestmark = pytest.mark.integration


request = evaluation_request


def count(lab, model) -> int:
    with session_scope(lab.factory) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def errors(lab, evaluation_id):
    with session_scope(lab.factory) as session:
        return session.scalars(
            select(StrategyEvaluationErrorRecord).where(
                StrategyEvaluationErrorRecord.evaluation_id == evaluation_id
            )
        ).all()


@pytest.fixture
def lab(fresh_engine):
    return make_lab(
        fresh_engine,
        [ProposingStrategy(), FailingStrategy(), UntraceableStrategy(), NewsFixtureStrategy()],
    )


@pytest.fixture
def asset(lab):
    asset_id = make_asset(lab.factory, "FXA")
    seed_bars(lab.factory, asset_id, count=40)
    return asset_id


def test_evaluation_records_a_complete_traceable_proposal(lab, asset):
    result = lab.engine.evaluate(request(lab, asset))
    assert result.outcome == "EVALUATED"
    assert result.status is EvaluationStatus.PROPOSED
    assert len(result.proposal_ids) == 1
    with session_scope(lab.factory) as session:
        evaluation = session.get(StrategyEvaluationRecord, result.evaluation_id)
        proposal = session.get(StrategyProposalRecord, result.proposal_ids[0])
        snapshot_ids = session.scalars(
            select(StrategyEvaluationSnapshotRecord.snapshot_id).where(
                StrategyEvaluationSnapshotRecord.evaluation_id == result.evaluation_id
            )
        ).all()
        snapshot = session.get(StrategyFeatureSnapshotRecord, snapshot_ids[0])
    assert evaluation is not None
    assert proposal is not None
    assert snapshot is not None
    assert evaluation.status is EvaluationStatus.PROPOSED
    assert evaluation.data_cutoff == NOW == evaluation.known_at  # LIVE: RECORDED at claim
    assert evaluation.finished_at == NOW
    # Every field of the standardized proposal.
    assert proposal.strategy_id == "fixture_proposer"
    assert proposal.strategy_version_id == lab.version_id("fixture_proposer")
    assert proposal.asset_id == asset
    assert proposal.market == "EQUITY"
    assert proposal.direction is Direction.LONG  # rising MOCK series: close above SMA
    assert proposal.timeframe == "1Day"
    assert proposal.signal_at == proposal.data_cutoff == NOW
    assert proposal.feature_snapshot_ids == [snapshot.id]
    newest = bar_close(39)
    assert proposal.reference_entry == newest
    assert proposal.invalidation_level is not None
    assert proposal.target_reference is not None
    assert proposal.invalidation_level < proposal.reference_entry < proposal.target_reference
    assert proposal.holding_horizon_periods == 5
    assert proposal.signal_strength == Decimal("0.5000")
    assert proposal.strength_kind is StrengthKind.UNCALIBRATED_SCORE
    assert {e["feature_key"] for e in proposal.evidence} == {
        "close.v1()@1Day",
        "sma.v1(length=5)@1Day",
    }
    assert proposal.reasoning
    # Expiry: one period after the signal = the close of the next session (Wed 4 Nov).
    assert proposal.expires_at == sessions(NOW + timedelta(days=1, hours=1), 1)[0].regular_close
    # The snapshot holds the values with their availability, all before the cutoff.
    assert snapshot.valid_count == snapshot.feature_count == 2
    assert snapshot.inputs_available_at is not None
    assert snapshot.last_period_event_at is not None
    assert snapshot.inputs_available_at <= NOW
    assert snapshot.last_period_event_at <= NOW
    assert snapshot.features["close.v1()@1Day"]["value"] == repr(float(newest))


def test_duplicate_update_is_evaluated_once(lab, asset):
    first = lab.engine.evaluate(request(lab, asset))
    lab.clock.advance(timedelta(minutes=5))
    again = lab.engine.evaluate(request(lab, asset))
    assert again.outcome == "DUPLICATE"
    assert again.evaluation_id == first.evaluation_id
    assert count(lab, StrategyEvaluationRecord) == 1
    assert count(lab, StrategyProposalRecord) == 1


def test_concurrent_identical_requests_produce_one_evaluation(lab, asset):
    req = request(lab, asset)
    results: list[Any] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        results.append(lab.engine.evaluate(req))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("EVALUATED") == 1
    assert set(outcomes) <= {"EVALUATED", "DUPLICATE", "IN_PROGRESS"}
    assert count(lab, StrategyEvaluationRecord) == 1
    assert count(lab, StrategyProposalRecord) == 1


def test_concurrent_batch_isolates_failures(lab, asset):
    other = make_asset(lab.factory, "FXB")
    seed_bars(lab.factory, other, count=40, step=-0.5)  # falling: SHORT
    requests = [
        request(lab, asset),
        request(lab, other),
        request(lab, asset, "fixture_failing"),
        request(lab, asset, "fixture_untraceable"),
        request(lab, asset),  # duplicate within the batch
    ]
    results = asyncio.run(lab.engine.evaluate_many(requests))
    by_strategy = {(r.request.strategy_id, r.request.asset_id): r for r in results[:4]}
    assert by_strategy["fixture_proposer", asset].status is EvaluationStatus.PROPOSED
    assert by_strategy["fixture_proposer", other].status is EvaluationStatus.PROPOSED
    failing = by_strategy["fixture_failing", asset]
    untraceable = by_strategy["fixture_untraceable", asset]
    assert failing.status is EvaluationStatus.FAILED
    assert untraceable.status is EvaluationStatus.FAILED
    assert results[4].outcome in {"DUPLICATE", "IN_PROGRESS", "EVALUATED"}
    assert count(lab, StrategyProposalRecord) == 2
    [failure] = errors(lab, failing.evaluation_id)
    assert failure.stage is ErrorStage.STRATEGY
    assert failure.error_type == "RuntimeError"
    assert "abc123" not in failure.message  # redacted
    [invalid] = errors(lab, untraceable.evaluation_id)
    assert invalid.stage is ErrorStage.VALIDATION
    assert "not in the evaluation's feature snapshot" in invalid.message
    with session_scope(lab.factory) as session:
        short = session.scalars(
            select(StrategyProposalRecord).where(StrategyProposalRecord.asset_id == other)
        ).one()
    assert short.direction is Direction.SHORT
    assert short.invalidation_level is not None
    assert short.target_reference is not None
    assert short.invalidation_level > short.reference_entry > short.target_reference


def test_failed_evaluations_are_retried_then_given_up(lab, asset):
    req = request(lab, asset, "fixture_failing")
    attempts = [lab.engine.evaluate(req) for _ in range(4)]
    assert [a.status for a in attempts[:3]] == [EvaluationStatus.FAILED] * 3
    assert attempts[3].outcome == "GAVE_UP"
    with session_scope(lab.factory) as session:
        rows = session.scalars(
            select(StrategyEvaluationRecord.attempt).order_by(StrategyEvaluationRecord.attempt)
        ).all()
    assert rows == [1, 2, 3]


def test_timeout_fails_the_evaluation_and_discards_the_late_result(fresh_engine):
    lab = make_lab(fresh_engine, [SlowStrategy()], evaluation_timeout_seconds=1)
    asset_id = make_asset(lab.factory, "FXS")
    seed_bars(lab.factory, asset_id, count=40)
    [result] = asyncio.run(lab.engine.evaluate_many([request(lab, asset_id, "fixture_slow")]))
    assert result.status is EvaluationStatus.FAILED
    [error] = errors(lab, result.evaluation_id)
    assert error.stage is ErrorStage.TIMEOUT
    # The strategy thread finishes later; its result must not be committed.
    threading.Event().wait(SlowStrategy.delay + 0.5)
    assert count(lab, StrategyProposalRecord) == 0
    with session_scope(lab.factory) as session:
        timed_out = session.get(StrategyEvaluationRecord, result.evaluation_id)
        assert timed_out is not None
        assert timed_out.status is (EvaluationStatus.FAILED)


def test_interrupted_evaluation_is_reclaimed_after_its_lease(lab, asset):
    req = request(lab, asset)
    claim = lab.engine.claim(req)  # a worker claims, then "dies" before running
    assert isinstance(claim, Claim)
    assert lab.engine.evaluate(req).outcome == "IN_PROGRESS"
    lab.clock.advance(timedelta(seconds=61))  # past the 60 s lease
    retried = lab.engine.evaluate(req)
    assert retried.status is EvaluationStatus.PROPOSED
    [interrupted] = errors(lab, claim.evaluation_id)
    assert interrupted.stage is ErrorStage.INTERRUPTED
    # The dead worker's late finish is discarded: it no longer owns a RUNNING evaluation.
    assert lab.engine.run(claim).outcome == "DISCARDED"
    assert count(lab, StrategyProposalRecord) == 1


def test_missing_bar_in_window_skips_without_calling_the_strategy(fresh_engine):
    lab = make_lab(fresh_engine, [FailingStrategy()])
    asset_id = make_asset(lab.factory, "FXM")
    seed_bars(lab.factory, asset_id, count=40, skip={37})  # a gap inside the SMA window
    result = lab.engine.evaluate(request(lab, asset_id, "fixture_failing"))
    assert result.status is EvaluationStatus.SKIPPED  # FailingStrategy was never called
    with session_scope(lab.factory) as session:
        evaluation = session.get(StrategyEvaluationRecord, result.evaluation_id)
    assert evaluation is not None
    reasons = {r["feature"]: r["status"] for r in evaluation.skip_reasons}
    assert reasons == {"sma.v1(length=5)@1Day": "MISSING_INPUTS"}
    assert errors(lab, result.evaluation_id) == []


def test_missing_news_coverage_blocks_the_event_strategy(lab, asset):
    result = lab.engine.evaluate(request(lab, asset, "fixture_news"))
    assert result.status is EvaluationStatus.SKIPPED
    with session_scope(lab.factory) as session:
        evaluation = session.get(StrategyEvaluationRecord, result.evaluation_id)
    assert evaluation is not None
    [reason] = evaluation.skip_reasons
    assert reason["feature"].startswith("news_event_count.v1(")
    assert reason["status"] == "UNAVAILABLE"


def test_recorded_inputs_exclude_bars_available_after_the_cutoff(lab):
    asset_id = make_asset(lab.factory, "FXL")
    days = seed_bars(lab.factory, asset_id, count=39, as_of=NOW - timedelta(days=1))
    # Today's bar exists but only became available after the decision time below.
    late = NOW + timedelta(hours=2)
    seed_bars(lab.factory, asset_id, count=1, available_at=late, start=500.0)
    cutoff = NOW
    result = lab.engine.evaluate(
        request(
            lab,
            asset_id,
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECORDED,
            data_cutoff=cutoff,
            trigger=TriggerKind.MANUAL,
            trigger_ref="test",
        )
    )
    # The newest completed session (3 Nov) has no bar known at the cutoff: missing data.
    assert result.status is EvaluationStatus.SKIPPED
    assert len(days) == 39
    # Reconstructed later, the same cutoff sees the late bar (labelled RECONSTRUCTED).
    lab.clock.set(late + timedelta(minutes=1))
    rebuilt = lab.engine.evaluate(
        request(
            lab,
            asset_id,
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECONSTRUCTED,
            data_cutoff=cutoff,
            trigger=TriggerKind.MANUAL,
            trigger_ref="test",
        )
    )
    assert rebuilt.status is EvaluationStatus.PROPOSED
    with session_scope(lab.factory) as session:
        proposal = session.get(StrategyProposalRecord, rebuilt.proposal_ids[0])
        assert proposal is not None
        snapshot = session.get(StrategyFeatureSnapshotRecord, proposal.feature_snapshot_ids[0])
        assert snapshot is not None
    assert proposal.basis is KnowledgeBasis.RECONSTRUCTED
    assert proposal.mode is EvaluationMode.HISTORICAL
    assert snapshot.known_at == late + timedelta(minutes=1)
    assert snapshot.inputs_available_at == late  # honest: known only later
    assert proposal.reference_entry == bar_close(0, start=500.0)


def test_unfinished_session_is_never_an_input(lab):
    asset_id = make_asset(lab.factory, "FXU")
    seed_bars(lab.factory, asset_id, count=40)
    # 3 Nov 20:00 UTC: the session is still open. Even reconstructed later (when its bar
    # is stored), the unfinished session is not part of the history: the newest is 2 Nov.
    cutoff = NOW.replace(hour=20, minute=0)
    result = lab.engine.evaluate(
        request(
            lab,
            asset_id,
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECONSTRUCTED,
            data_cutoff=cutoff,
            trigger=TriggerKind.MANUAL,
            trigger_ref="intraday",
        )
    )
    with session_scope(lab.factory) as session:
        proposal = session.get(StrategyProposalRecord, result.proposal_ids[0])
        assert proposal is not None
        snapshot = session.get(StrategyFeatureSnapshotRecord, proposal.feature_snapshot_ids[0])
        assert snapshot is not None
    assert proposal.reference_entry == bar_close(38)  # not the 3 Nov bar (index 39)
    assert snapshot.last_period_event_at is not None
    assert snapshot.last_period_event_at < cutoff.replace(hour=5)


def test_identical_inputs_share_one_snapshot(lab, asset):
    a = lab.engine.evaluate(
        request(
            lab,
            asset,
            mode=EvaluationMode.HISTORICAL,
            data_cutoff=NOW - timedelta(minutes=10),
            trigger=TriggerKind.MANUAL,
            trigger_ref="a",
        )
    )
    b = lab.engine.evaluate(
        request(
            lab,
            asset,
            mode=EvaluationMode.HISTORICAL,
            data_cutoff=NOW - timedelta(minutes=10),
            trigger=TriggerKind.MANUAL,
            trigger_ref="b",
        )
    )
    assert a.evaluation_id != b.evaluation_id
    assert count(lab, StrategyFeatureSnapshotRecord) == 1


def test_proposals_expire_and_are_superseded(lab, asset):
    first = lab.engine.evaluate(request(lab, asset))
    lab.clock.advance(timedelta(hours=1))
    second = lab.engine.evaluate(request(lab, asset, trigger_ref="1Day:again"))
    with session_scope(lab.factory) as session:
        [event] = session.scalars(select(StrategyProposalEventRecord)).all()
        views = {v.id: v.status for v in queries.proposals(session, lab.clock.now(), limit=10)}
    assert event.proposal_id == first.proposal_ids[0]
    assert event.kind is ProposalEventKind.SUPERSEDED
    assert event.superseded_by == second.proposal_ids[0]
    assert views == {
        first.proposal_ids[0]: ProposalStatus.SUPERSEDED,
        second.proposal_ids[0]: ProposalStatus.ACTIVE,
    }
    # After the expiry the active one expires; recording it is idempotent.
    lab.clock.advance(timedelta(days=2))
    assert lab.engine.expire_due() == 1
    assert lab.engine.expire_due() == 0
    with session_scope(lab.factory) as session:
        statuses = {v.id: v.status for v in queries.proposals(session, lab.clock.now(), limit=10)}
        kinds = sorted(e.kind for e in session.scalars(select(StrategyProposalEventRecord)))
    assert statuses[second.proposal_ids[0]] is ProposalStatus.EXPIRED
    assert kinds == [ProposalEventKind.EXPIRED, ProposalEventKind.SUPERSEDED]


def test_results_and_proposals_cannot_be_rewritten(lab, asset):
    result = lab.engine.evaluate(request(lab, asset))
    with pytest.raises(DBAPIError, match="final"), session_scope(lab.factory) as session:
        session.execute(
            update(StrategyEvaluationRecord)
            .where(StrategyEvaluationRecord.id == result.evaluation_id)
            .values(status=EvaluationStatus.NO_PROPOSAL, proposals_count=0)
        )
    with pytest.raises(DBAPIError, match="append-only"), session_scope(lab.factory) as session:
        session.execute(
            update(StrategyProposalRecord)
            .where(StrategyProposalRecord.id == result.proposal_ids[0])
            .values(reasoning="rewritten")
        )
    with pytest.raises(DBAPIError, match="never deleted"), session_scope(lab.factory) as session:
        session.execute(text("DELETE FROM strategy_evaluations"))
    with pytest.raises(DBAPIError, match="append-only"), session_scope(lab.factory) as session:
        session.execute(text("DELETE FROM strategy_feature_snapshots"))


def test_running_evaluation_identity_cannot_change(lab, asset):
    claim = lab.engine.claim(request(lab, asset))
    with pytest.raises(DBAPIError, match="only the outcome"), session_scope(lab.factory) as s:
        s.execute(
            update(StrategyEvaluationRecord)
            .where(StrategyEvaluationRecord.id == claim.evaluation_id)
            .values(data_cutoff=NOW - timedelta(days=1))
        )


def test_plan_selects_enabled_implemented_matching_strategies(lab, asset):
    etf = make_asset(lab.factory, "FXE", asset_class=AssetClass.ETF)
    lab.enable("fixture_untraceable", False)
    updates = [
        MarketUpdate(asset, TriggerKind.BAR_CLOSE, "1Day:2026-11-03", "1Day"),
        MarketUpdate(asset, TriggerKind.NEWS_EVENT, f"news:{uuid.uuid4()}:1"),
        MarketUpdate(etf, TriggerKind.BAR_CLOSE, "1Day:2026-11-03", "1Day"),  # not EQUITY
        MarketUpdate(asset, TriggerKind.BAR_CLOSE, "1Week:2026-W45", "1Week"),  # not configured
    ]
    planned = {(r.strategy_id, r.trigger) for r in lab.engine.plan(updates)}
    assert planned == {
        ("fixture_proposer", TriggerKind.BAR_CLOSE),
        ("fixture_failing", TriggerKind.BAR_CLOSE),
        ("fixture_news", TriggerKind.BAR_CLOSE),
        ("fixture_news", TriggerKind.NEWS_EVENT),
    }


def test_live_requests_cannot_choose_their_cutoff(lab, asset):
    with pytest.raises(ValueError, match="claim time"):
        lab.engine.claim(request(lab, asset, data_cutoff=NOW - timedelta(hours=1)))
    with pytest.raises(ValueError, match="past data cutoff"):
        lab.engine.claim(
            request(lab, asset, mode=EvaluationMode.HISTORICAL, data_cutoff=NOW + timedelta(1))
        )

"""Phase 10A orchestration against a real PostgreSQL (TEST FIXTURE data, simulated orders).

Operator controls, the intent inbox (intake, risk reservation, simulated paper order),
idempotency across ticks, restarts and concurrent orchestrators, crash and retry behaviour,
the emergency stop, and the durable scheduled jobs.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import AtlasError
from app.database.models import AuditLog
from app.database.models.execution import OrderRecord, OutboxEventRecord
from app.database.models.orchestration import (
    OrchestrationJobRecord,
    TradeIntentRecord,
    TradingControlEventRecord,
)
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationRecord,
)
from app.database.sessions import session_scope
from app.execution.enums import OrderPurpose, OrderStatus, OrderType
from app.orchestration import controls
from app.orchestration.controls import ControlError
from app.orchestration.enums import (
    ControlAction,
    IntentStatus,
    JobKind,
    JobStatus,
    TradingMode,
)
from app.orchestration.intents import INTENT_TOPIC, ORCHESTRATOR_ACTOR, take_in
from app.orchestration.jobs import JobRunner, JobSpec, every
from app.risk.enums import ReservationStatus
from app.strategies.enums import StrategyFamily
from app.tests.integration.ai_support import make_proposal, make_strategy
from app.tests.integration.orchestration_support import OPERATOR, Bench, make_bench

pytestmark = pytest.mark.integration


@pytest.fixture
def bench(fresh_engine) -> Bench:
    return make_bench(fresh_engine)


def tick(runtime: Any) -> Any:
    return asyncio.run(runtime.orchestrator.tick())


# ====================================================================== controls
def test_trading_is_disabled_by_default_and_nothing_is_ordered(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    assert bench.mode() is TradingMode.DISABLED
    with session_scope(bench.factory) as session:
        state = controls.current(session, bench.account)
    assert state.sequence == 0
    assert state.as_dict()["default"] is True
    runtime = bench.orchestrator()
    report = tick(runtime)
    assert report.mode == "DISABLED"
    assert report.intents_taken == 0
    assert bench.intents() == []
    assert bench.reservations() == []
    assert bench.orders() == []


def test_control_transitions_are_operator_only_audited_and_gapless(bench):
    svc = bench.control.controls
    with pytest.raises(ControlError, match="not allowed while trading is DISABLED"):
        svc.change(bench.account, ControlAction.RESUME, actor=OPERATOR, reason="resume")
    with pytest.raises(AtlasError, match="operator"):
        svc.change(bench.account, ControlAction.ENABLE, actor="ai:coordinator", reason="go go go")
    with pytest.raises(AtlasError, match="operator"):
        svc.change(bench.account, ControlAction.ENABLE, actor="system:orchestrator", reason="auto")
    with pytest.raises(ControlError, match="reason"):
        svc.change(bench.account, ControlAction.ENABLE, actor=OPERATOR, reason=" ")
    assert bench.mode() is TradingMode.DISABLED

    bench.enable()
    assert bench.mode() is TradingMode.ENABLED
    again = svc.change(bench.account, ControlAction.ENABLE, actor=OPERATOR, reason="again")
    assert again.changed is False  # repeating what is in effect is not an error
    bench.set_mode(ControlAction.PAUSE)
    bench.set_mode(ControlAction.RESUME)
    bench.set_mode(ControlAction.DISABLE)
    with pytest.raises(ControlError):
        svc.change(bench.account, ControlAction.PAUSE, actor=OPERATOR, reason="pause")
    with pytest.raises(ControlError, match="emergency_stop"):
        svc.change(bench.account, ControlAction.EMERGENCY_STOP, actor=OPERATOR, reason="stop")

    with session_scope(bench.factory) as session:
        history = controls.history(session, bench.account)
        audits = session.scalars(
            select(AuditLog.action).where(AuditLog.action.like("orchestration.trading_%"))
        ).all()
    assert [(h["sequence"], h["action"], h["to_mode"]) for h in reversed(history)] == [
        (1, "ENABLE", "ENABLED"),
        (2, "PAUSE", "PAUSED"),
        (3, "RESUME", "ENABLED"),
        (4, "DISABLE", "DISABLED"),
    ]
    assert len(audits) == 4

    # The database itself keeps the history append-only, numbered and continuous.
    with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
        session.execute(
            update(TradingControlEventRecord)
            .where(TradingControlEventRecord.portfolio_account_id == bench.account)
            .values(reason="rewritten")
        )
    with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
        session.add(
            TradingControlEventRecord(
                portfolio_account_id=bench.account,
                sequence=9,
                action=ControlAction.ENABLE,
                from_mode=TradingMode.DISABLED,
                to_mode=TradingMode.ENABLED,
                actor=OPERATOR,
                reason="skips numbers",
                occurred_at=bench.now(),
            )
        )
    with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
        session.add(
            TradingControlEventRecord(
                portfolio_account_id=bench.account,
                sequence=5,
                action=ControlAction.RESUME,
                from_mode=TradingMode.PAUSED,  # the previous event ended DISABLED
                to_mode=TradingMode.ENABLED,
                actor=OPERATOR,
                reason="wrong starting mode",
                occurred_at=bench.now(),
            )
        )


def test_concurrent_control_changes_serialize(bench):
    svc = bench.control.controls
    errors: list[BaseException] = []

    def flip(action: ControlAction) -> None:
        try:
            if action is ControlAction.EMERGENCY_STOP:
                svc.emergency_stop(bench.account, actor=OPERATOR, reason="concurrent stop")
            else:
                svc.change(bench.account, action, actor=OPERATOR, reason="concurrent change")
        except ControlError:
            pass  # refused transitions are expected
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=flip, args=(a,))
        for a in [ControlAction.ENABLE] * 4 + [ControlAction.EMERGENCY_STOP] * 2
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with session_scope(bench.factory) as session:
        sequences = session.scalars(
            select(TradingControlEventRecord.sequence)
            .where(TradingControlEventRecord.portfolio_account_id == bench.account)
            .order_by(TradingControlEventRecord.sequence)
        ).all()
    assert list(sequences) == list(range(1, len(sequences) + 1))
    assert bench.mode() is TradingMode.EMERGENCY_STOP


# ====================================================================== intents
def test_enabled_orchestrator_reserves_and_orders_through_the_risk_engine(bench):
    asset = bench.asset("ORCA")
    decision = bench.decision(asset)
    bench.enable()
    report = tick(bench.orchestrator())
    assert report.mode == "ENABLED"
    assert (report.intents_taken, report.intents_delivered) == (1, 1)

    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert intent.ai_decision_id == decision
    assert intent.trading_mode is TradingMode.ENABLED
    [reservation] = bench.reservations()
    assert reservation.id == intent.reservation_id
    assert reservation.ai_decision_id == decision
    assert reservation.status is ReservationStatus.APPROVED
    [order] = bench.orders()
    assert order.id == intent.order_id
    assert order.purpose is OrderPurpose.ENTRY
    assert order.order_type is OrderType.LIMIT
    assert order.requested_by == ORCHESTRATOR_ACTOR
    assert order.status is OrderStatus.APPROVED
    with session_scope(bench.factory) as session:
        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == reservation.id
            )
        )
        assert approval is not None
        # Quantity and price come from the risk engine's approval, nothing else.
        assert order.quantity == approval.approved_quantity
        assert order.limit_price == approval.worst_entry_price
    assert order.expires_at is not None
    assert order.expires_at > bench.now()


def test_intake_and_delivery_are_idempotent_across_ticks_restarts_and_orchestrators(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    first = bench.orchestrator(worker_id="orchestrator:a")
    second = bench.orchestrator(worker_id="orchestrator:b")
    errors: list[BaseException] = []

    def run(runtime: Any) -> None:
        try:
            for _ in range(3):
                tick(runtime)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(r,)) for r in (first, second, first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    tick(bench.orchestrator(worker_id="orchestrator:restarted"))  # a restart
    assert len(bench.intents()) == 1
    assert len(bench.reservations()) == 1
    assert len(bench.orders()) == 1
    assert bench.count(OutboxEventRecord, OutboxEventRecord.topic == INTENT_TOPIC) == 1


@pytest.mark.parametrize(
    "fixture",
    [
        {"outcome": "REJECT"},
        {"mode": "HISTORICAL"},
        {"run_status": "FAILED"},
        {"age": timedelta(hours=5)},  # older than the intake lookback (120 min)
        {"age": timedelta(minutes=30), "lifetime": timedelta(minutes=20)},  # expired
    ],
)
def test_only_fresh_prioritized_live_decisions_are_taken_in(bench, fixture):
    asset = bench.asset("ORCA")
    bench.decision(asset, **fixture)
    bench.enable()
    report = tick(bench.orchestrator())
    assert report.intents_taken == 0
    assert bench.intents() == []
    assert bench.orders() == []


def test_mode_change_before_delivery_records_not_traded(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    with session_scope(bench.factory) as session:
        [intent_id] = take_in(
            session, bench.account, now=bench.now(), lookback=timedelta(hours=2), limit=10
        )
    bench.set_mode(ControlAction.PAUSE)
    report = tick(bench.orchestrator())
    assert report.intents_taken == 0  # paused: no intake
    assert report.intents_delivered == 1  # but the pending intent is resolved
    [intent] = bench.intents()
    assert intent.id == intent_id
    assert intent.status is IntentStatus.NOT_TRADED
    assert intent.reasons[0]["code"] == "TRADING_PAUSED"
    assert bench.reservations() == []
    assert bench.orders() == []


def test_risk_rejection_is_a_no_trade_outcome(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    bench.control.risk.service.engage_kill_switch(
        bench.account, actor=OPERATOR, reason="halt for the test"
    )
    tick(bench.orchestrator())
    [intent] = bench.intents()
    assert intent.status is IntentStatus.RISK_REJECTED
    assert intent.order_id is None
    assert bench.orders() == []
    assert all(r.status is not ReservationStatus.APPROVED for r in bench.reservations())


def test_a_crash_during_delivery_rolls_back_and_the_retry_creates_exactly_one_order(
    bench, monkeypatch
):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator()
    execution = runtime.orchestrator.intents._execution
    real = execution.submit_in
    calls = {"n": 0}

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("simulated crash after the reservation (TEST FIXTURE)")
        return real(*args, **kwargs)

    monkeypatch.setattr(execution, "submit_in", crash_once)
    report = tick(runtime)
    assert report.intents_failed == 1
    [intent] = bench.intents()
    assert intent.status is IntentStatus.RECEIVED
    assert bench.reservations() == []  # rolled back with the failed attempt
    assert bench.orders() == []
    bench.clock.advance(timedelta(minutes=1))  # past the outbox back-off
    tick(runtime)
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert len(bench.reservations()) == 1
    assert len(bench.orders()) == 1


def test_delivery_failing_every_attempt_is_dead_with_nothing_reserved(bench, monkeypatch):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator(intent_max_attempts=2)
    execution = runtime.orchestrator.intents._execution

    def always(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("execution engine unavailable (TEST FIXTURE)")

    monkeypatch.setattr(execution, "submit_in", always)
    tick(runtime)
    bench.clock.advance(timedelta(minutes=1))
    report = tick(runtime)
    assert report.intents_dead == 1
    [intent] = bench.intents()
    assert intent.status is IntentStatus.DEAD
    assert "execution engine unavailable" in (intent.error or "")
    assert bench.reservations() == []
    assert bench.orders() == []
    [event] = bench.health_events("TRADE_INTENT_DEAD")
    assert event.severity.value == "HIGH"


def test_final_intents_never_change(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    tick(bench.orchestrator())
    [intent] = bench.intents()
    with pytest.raises(DBAPIError, match="final"), session_scope(bench.factory) as session:
        session.execute(
            update(TradeIntentRecord)
            .where(TradeIntentRecord.id == intent.id)
            .values(status=IntentStatus.RECEIVED, decided_at=None)
        )
    with pytest.raises(DBAPIError, match="never deleted"), session_scope(bench.factory) as s:
        s.execute(text("DELETE FROM trade_intents"))


# ====================================================================== emergency stop
def _position_with_open_exit(bench: Bench) -> tuple[uuid.UUID, uuid.UUID]:
    """An operator entry filled at the paper broker (controlled test fill) and an exit order
    working for half of it: (entry order, exit order)."""
    ex = bench.ex
    asset = ex.asset("EXIT")
    reservation = ex.reservation(bench.account, asset, "orch-exit-res")
    entry = ex.open_order(bench.account, reservation, "10", "orch-exit-entry")
    ex.fill(entry, "10", "99.70", "orch-exit-fill")
    exit_result = ex.submit(ex.exit(bench.account, entry, "5"), "orch-exit-1")
    assert exit_result.order_id is not None, exit_result.reasons
    ex.dispatch()
    assert ex.order(exit_result.order_id).status is OrderStatus.OPEN
    return entry, exit_result.order_id


def test_emergency_stop_cancels_entries_engages_kill_switch_and_keeps_exits(bench):
    entry, exit_order = _position_with_open_exit(bench)
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    tick(bench.orchestrator())
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    bench.ex.dispatch()  # the paper broker accepts the autonomous entry
    assert bench.ex.order(intent.order_id).status is OrderStatus.OPEN

    report = bench.control.controls.emergency_stop(
        bench.account, actor=OPERATOR, reason="emergency drill"
    )
    assert report.changed is True
    assert report.kill_switch_engaged_now is True
    assert report.cancel_requested == [str(intent.order_id)]
    assert bench.mode() is TradingMode.EMERGENCY_STOP
    bench.ex.dispatch()
    assert bench.ex.order(intent.order_id).status is OrderStatus.CANCELLED
    # Exits are never cancelled, and risk-reducing exits can still be submitted.
    assert bench.ex.order(exit_order).status is OrderStatus.OPEN
    second_exit = bench.ex.submit(bench.ex.exit(bench.account, entry, "5"), "orch-exit-2")
    assert second_exit.order_id is not None, second_exit.reasons
    # The autonomous entry's reservation was released by the cancellation.
    with session_scope(bench.factory) as session:
        r = session.get(RiskReservationRecord, intent.reservation_id)
        assert r is not None
        assert r.status is ReservationStatus.RELEASED

    # No new intake while stopped; repeating the stop is safe.
    bench.decision(asset)
    assert tick(bench.orchestrator()).intents_taken == 0
    again = bench.control.controls.emergency_stop(
        bench.account, actor=OPERATOR, reason="emergency drill again"
    )
    assert again.changed is False
    assert again.kill_switch_engaged_now is False
    # Clearing returns to DISABLED, never straight to ENABLED.
    bench.set_mode(ControlAction.CLEAR_EMERGENCY)
    assert bench.mode() is TradingMode.DISABLED


# ====================================================================== scheduled jobs
def _runner(bench: Bench, owner: str, **kw: Any) -> JobRunner:
    return JobRunner(
        bench.factory,
        bench.clock,
        owner=owner,
        lease=kw.get("lease", timedelta(minutes=10)),
        max_attempts=kw.get("max_attempts", 3),
        account_id=bench.account,
    )


def _jobs(bench: Bench) -> list[OrchestrationJobRecord]:
    with session_scope(bench.factory) as session:
        rows = session.scalars(select(OrchestrationJobRecord).order_by(OrchestrationJobRecord.id))
        out = list(rows.all())
        session.expunge_all()
        return out


def test_a_slot_runs_once_however_many_runners_tick(bench):
    calls: list[str] = []
    lock = threading.Lock()

    def body() -> dict[str, Any]:
        with lock:
            calls.append("ran")
        return {"ok": True}

    spec = JobSpec(JobKind.RISK_MONITOR, every(300), body)
    runners = [_runner(bench, f"runner-{i}") for i in range(4)]
    threads = [threading.Thread(target=r.run_due, args=([spec],)) for r in runners * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["ran"]
    [job] = _jobs(bench)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"ok": True}
    bench.clock.advance(timedelta(minutes=5))  # the next interval is a new slot
    runners[0].run_due([spec])
    assert len(calls) == 2


def test_failed_jobs_retry_with_backoff_then_die_and_alert(bench):
    attempts = {"n": 0}

    def flaky() -> dict[str, Any]:
        attempts["n"] += 1
        raise RuntimeError("reconciliation source unavailable (TEST FIXTURE)")

    spec = JobSpec(JobKind.ACCOUNTING_RECONCILE, every(3600), flaky)
    runner = _runner(bench, "runner", max_attempts=3)
    [out] = runner.run_due([spec])
    assert out.status is JobStatus.FAILED
    assert out.attempts == 1
    assert runner.run_due([spec]) == []  # not before the back-off
    bench.clock.advance(timedelta(seconds=31))
    [out] = runner.run_due([spec])
    assert out.status is JobStatus.FAILED
    assert out.attempts == 2
    bench.clock.advance(timedelta(seconds=61))
    [out] = runner.run_due([spec])
    assert out.status is JobStatus.DEAD
    assert out.attempts == 3
    bench.clock.advance(timedelta(minutes=10))
    assert runner.run_due([spec]) == []  # dead jobs are not retried
    assert attempts["n"] == 3
    [event] = bench.health_events("ORCHESTRATION_JOB_DEAD")
    assert "TEST FIXTURE" in event.summary


def test_a_stuck_job_is_reclaimed_after_its_lease_expires(bench):
    spec_calls = {"n": 0}

    def body() -> dict[str, Any]:
        spec_calls["n"] += 1
        return {}

    spec = JobSpec(JobKind.EXECUTION_RECONCILE, every(3600), body)
    crashed = _runner(bench, "crashed-worker", lease=timedelta(minutes=2))
    claimed = crashed.claim(JobKind.EXECUTION_RECONCILE, spec.slot(bench.now()) or "")
    assert claimed is not None  # ... and the worker dies before finishing
    other = _runner(bench, "healthy-worker")
    assert other.run_due([spec]) == []  # the lease is still valid
    bench.clock.advance(timedelta(minutes=3))
    [out] = other.run_due([spec])
    assert out.status is JobStatus.SUCCEEDED
    assert out.reclaimed is True
    assert spec_calls["n"] == 1
    [job] = _jobs(bench)
    assert job.reclaimed == 1
    assert job.attempts == 2
    assert bench.health_events("ORCHESTRATION_JOB_STUCK")


def test_sweep_finishes_stuck_jobs_of_past_slots(bench):
    crashed = _runner(bench, "crashed-worker", lease=timedelta(minutes=2))
    assert crashed.claim(JobKind.RISK_MONITOR, "2026-11-03T22:00:00Z") is not None
    bench.clock.advance(timedelta(minutes=3))
    assert _runner(bench, "sweeper").sweep() == 1
    [job] = _jobs(bench)
    assert job.status is JobStatus.FAILED
    assert job.lease_owner is None
    assert "crashed-worker" in (job.error or "")


def test_scheduled_work_runs_on_real_engines_and_reconciles_clean_books(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator()
    report = tick(runtime)
    assert report.errors == []
    statuses = {j.kind: j.status for j in report.jobs}
    assert statuses == {
        JobKind.RISK_MONITOR: JobStatus.SUCCEEDED,
        JobKind.RESERVATION_RECONCILE: JobStatus.SUCCEEDED,
        JobKind.EXECUTION_RECONCILE: JobStatus.SUCCEEDED,
        JobKind.ACCOUNTING_RECONCILE: JobStatus.SUCCEEDED,
        JobKind.WORKER_HEALTH: JobStatus.SUCCEEDED,
        # Phase 10B operational jobs (the fixed clock is after the close: the day's review
        # runs, and the reviews of the preceding trading days are caught up).
        JobKind.DATA_FRESHNESS: JobStatus.SUCCEEDED,
        JobKind.SESSION_CLOSE_REVIEW: JobStatus.SUCCEEDED,
    }
    results = {j.kind: j.result for j in _jobs(bench)}
    assert results[JobKind.RESERVATION_RECONCILE]["passed"] is True
    assert results[JobKind.EXECUTION_RECONCILE]["passed"] is True
    assert results[JobKind.ACCOUNTING_RECONCILE]["passed"] is True
    assert bench.health_events("RESERVATION_RECONCILIATION_FAILED") == []
    assert bench.health_events("ACCOUNTING_RECONCILIATION_FAILED") == []
    # A second tick in the same slots runs nothing again.
    assert tick(runtime).jobs == []


def test_research_stages_run_and_decisions_are_requested_only_for_new_proposals(bench):
    calls = bench.calls
    research = {
        "strategy_cycle": None,
        "decide": calls.stub("decide"),
        "market_intelligence": calls.stub("market_intelligence"),
        "decision_outcomes": calls.stub("decision_outcomes"),
    }

    async def cycle() -> dict[str, Any]:
        calls.counts["strategy"] = calls.counts.get("strategy", 0) + 1
        return {"fixture": True}

    research["strategy_cycle"] = cycle
    runtime = bench.orchestrator(
        research=research,
        run_strategy_stage=True,
        run_decision_stage=True,
        run_market_intelligence=True,
    )
    tick(runtime)
    assert calls.counts.get("strategy") == 1
    assert "decide" not in calls.counts  # no LIVE proposal exists
    asset = bench.asset("ORCA")
    bench.decision(asset)  # a LIVE run, whose lead proposal is older than the run
    tick(runtime)
    assert "decide" not in calls.counts  # nothing arrived since that run
    version = make_strategy(bench.factory, "momentum_fixture_new", StrategyFamily.MOMENTUM)
    make_proposal(  # a new LIVE proposal, stored now
        bench.factory,
        strategy_id="momentum_fixture_new",
        version_id=version,
        asset_id=asset,
        stored_at=bench.now(),
        cutoff=bench.now() - timedelta(hours=1),
        expires_at=bench.now() + timedelta(days=1),
    )
    bench.clock.advance(timedelta(seconds=1))
    tick(runtime)
    assert calls.counts.get("decide") == 1
    tick(runtime)
    assert calls.counts.get("decide") == 1  # not again within the minimum interval
    # After the close plus the delay, market intelligence runs once for the day.
    assert calls.counts.get("market_intelligence") == 1
    tick(runtime)
    assert calls.counts.get("market_intelligence") == 1


def test_a_failing_stage_degrades_the_tick_but_not_the_others(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator(
        research={"decide": bench.calls.stub("decide", fail=99)},
    )
    orch = runtime.orchestrator
    orch.stages.expire_reservations = bench.calls.stub("expire", fail=1)
    report = tick(runtime)
    assert any(e.startswith("expire_reservations:") for e in report.errors)
    assert report.intents_delivered == 1  # trading work went on
    with session_scope(bench.factory) as session:
        from app.database.models.orchestration import OrchestratorStatusRecord

        status = session.get(OrchestratorStatusRecord, "trading-orchestrator")
        assert status is not None
        assert status.state.value == "DEGRADED"
        assert "TEST FIXTURE" in (status.last_error or "")
    report = tick(runtime)
    assert report.errors == []
    with session_scope(bench.factory) as session:
        status = session.get(OrchestratorStatusRecord, "trading-orchestrator")
        assert status is not None
        assert status.state.value == "RUNNING"


def test_extended_operation_creates_no_duplicates(bench):
    """Many ticks over a simulated day, decisions arriving over time, mode changes and a
    restart: one intent, one reservation and at most one order per decision."""
    bench.enable()
    runtime = bench.orchestrator()
    decisions: list[uuid.UUID] = []
    for step in range(12):
        if step % 3 == 0:
            decisions.append(bench.decision(bench.asset(f"SK{step:02d}")))
        if step == 5:
            bench.set_mode(ControlAction.PAUSE)
        if step == 7:
            bench.set_mode(ControlAction.RESUME)
        if step == 8:
            runtime = bench.orchestrator(worker_id="orchestrator:after-restart")
        tick(runtime)
        bench.clock.advance(timedelta(minutes=2))
    intents = bench.intents()
    assert sorted(i.ai_decision_id for i in intents) == sorted(decisions)
    assert all(i.status is not IntentStatus.RECEIVED for i in intents)
    orders = bench.orders()
    assert len({o.ai_decision_id for o in orders}) == len(orders)
    reserved = [r for r in bench.reservations() if r.ai_decision_id is not None]
    assert len({r.ai_decision_id for r in reserved}) == len(reserved)
    ordered = {i.ai_decision_id for i in intents if i.status is IntentStatus.ORDER_CREATED}
    assert ordered == {o.ai_decision_id for o in orders}
    with session_scope(bench.factory) as session:
        quantities = session.scalars(select(OrderRecord.quantity)).all()
    assert all(q > Decimal(0) for q in quantities)
    # Every job slot exists once.
    jobs = _jobs(bench)
    assert len({(j.kind, j.slot) for j in jobs}) == len(jobs)

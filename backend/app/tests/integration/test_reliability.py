"""Phase 10B reliability and sustained operation on a real PostgreSQL.

TEST FIXTURE material only (see ``orchestration_support`` and ``simulation_support``):
CONTROLLED_TEST accounts with simulated capital, invented instruments, MOCK quotes and bars,
fixture strategy proposals and AI decisions. Every order is a simulated paper order of the
isolated paper broker; nothing reaches a brokerage. "Crashes" and "outages" are simulated:
a worker that stops renewing its heartbeat, a session that raises, database connections
terminated with ``pg_terminate_backend``, Redis commands that raise.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import redis
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.database.models import AuditLog
from app.database.models.execution import (
    OrderFillRecord,
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerEventRecord,
    PaperPositionRecord,
)
from app.database.models.orchestration import (
    OrchestrationJobRecord,
    OrchestratorStatusRecord,
    TradeIntentRecord,
)
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.database.sessions import session_scope
from app.events.outbox import enqueue
from app.execution.enums import (
    BrokerEventType,
    ExitReason,
    OrderPurpose,
    OutboxStatus,
    PositionStatus,
)
from app.execution.monitor import MonitorSummary, PositionMonitor
from app.execution.reconciliation import ExecutionReconciler
from app.execution.simulation import ExecutionSimulator
from app.monitoring import heartbeats
from app.monitoring.heartbeats import POSITION_MONITOR, HeartbeatWriter
from app.monitoring.workers import Switches, WorkerHealth, workers_report
from app.orchestration.enums import IntentStatus, JobKind, JobStatus, WorkerState
from app.orchestration.intents import INTENT_TOPIC, take_in
from app.orchestration.jobs import JobControlError, JobRunner, JobSpec, every
from app.orchestration.orchestrator import STATUS_NAME
from app.risk.enums import BreakerStatus, BreakerType, Severity
from app.risk.state import breaker_statuses
from app.tests.integration.orchestration_support import OPERATOR, Bench, make_bench
from app.tests.integration.simulation_support import SESSION, D, Sim, make_simulation
from app.workers import position_monitor

pytestmark = pytest.mark.integration


@pytest.fixture
def bench(fresh_engine) -> Bench:
    return make_bench(fresh_engine, name="reliability-test")


# ====================================================================== helpers
def tick(runtime: Any) -> Any:
    return asyncio.run(runtime.orchestrator.tick())


def sim_of(bench: Bench) -> Sim:
    """The 7B simulator and position monitor on the bench's execution engine (they play
    the paper execution worker and the position monitor worker)."""
    ex = bench.ex
    return Sim(
        ex,
        ExecutionSimulator(ex.service, bench.clock),
        PositionMonitor(ex.service, bench.clock, accounting=ex.rv.service._accounting),
    )


def beat_workers(bench: Bench, *, monitor: bool = True, broker: bool = True) -> None:
    """The workers that manage positions record that they are alive (now)."""
    if monitor:
        HeartbeatWriter(
            bench.factory, bench.clock, POSITION_MONITOR, interval_seconds=5, instance="fixture"
        ).beat(passes=1)
    if broker:
        with session_scope(bench.factory) as session:
            bench.ex.broker.heartbeat(session, bench.clock.now())


def open_long(bench: Bench, sim: Sim, symbol: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A filled long market entry of the bench account: 10 bought at 99.58, stop 94.70."""
    ex = bench.ex
    asset = ex.asset(symbol)
    r = ex.reservation(bench.account, asset, f"rel-res-{symbol}")
    entry = ex.open_order(bench.account, r, "10", f"rel-order-{symbol}")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    ex.dispatch()
    position = sim.position(entry)
    assert position is not None
    assert position.status is PositionStatus.OPEN
    return asset, entry


def exits(bench: Bench, entry: uuid.UUID) -> list[OrderRecord]:
    with session_scope(bench.factory) as session:
        rows = session.scalars(
            select(OrderRecord)
            .where(OrderRecord.parent_order_id == entry, OrderRecord.purpose == OrderPurpose.EXIT)
            .order_by(OrderRecord.submitted_at)
        ).all()
        session.expunge_all()
        return list(rows)


def jobs(bench: Bench, kind: JobKind) -> dict[str, OrchestrationJobRecord]:
    with session_scope(bench.factory) as session:
        rows = session.scalars(
            select(OrchestrationJobRecord).where(OrchestrationJobRecord.kind == kind)
        ).all()
        session.expunge_all()
        return {r.slot: r for r in rows}


def status_row(bench: Bench) -> OrchestratorStatusRecord:
    with session_scope(bench.factory) as session:
        row = session.get(OrchestratorStatusRecord, STATUS_NAME)
        assert row is not None
        session.expunge(row)
        return row


def runner(bench: Bench, owner: str, **kw: Any) -> JobRunner:
    values: dict[str, Any] = {"lease": timedelta(minutes=10), "max_attempts": 3}
    values.update(kw)
    return JobRunner(bench.factory, bench.clock, owner=owner, **values)


def books_reconcile(bench: Bench) -> None:
    """Ledger, reservations and execution reconcile for the bench account."""
    bench.ex.dispatch()
    assert bench.ex.risk.reconcile(bench.account) == "PASSED"
    result = ExecutionReconciler(bench.factory, bench.clock).run(bench.account)
    assert result.passed, result.findings


def terminate_connections(engine: Any) -> int:
    """Kill every other connection to the test database (a real interruption), from a
    separate short-lived engine so that no pooled connection of the application is used."""
    admin = create_engine(engine.url, poolclass=NullPool)
    try:
        with admin.connect() as conn:
            killed = conn.execute(
                text(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            ).scalar()
            conn.commit()
    finally:
        admin.dispose()
    return int(killed or 0)


# ====================================================================== entry gate
def test_the_entry_gate_refuses_entries_until_the_protection_workers_run(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    bench.clock.advance(timedelta(minutes=5))  # the broker's start-up heartbeat is stale
    runtime = bench.orchestrator(require_protection_workers=True)
    report = tick(runtime)
    assert report.gate_open is False
    assert {r["code"] for r in report.protection["reasons"]} == {
        "POSITION_MONITOR_DOWN",
        "PAPER_EXECUTION_DOWN",
    }
    assert bench.intents() == []
    assert bench.orders() == []
    assert status_row(bench).entry_gate_open is False
    [closed] = bench.health_events("ENTRY_GATE_CLOSED")
    assert closed.severity is Severity.HIGH
    assert "action" in closed.details
    [down] = bench.health_events("POSITION_MONITOR_DOWN")
    assert down.severity is Severity.WARNING  # nothing is open
    tick(runtime)
    assert len(bench.health_events("ENTRY_GATE_CLOSED")) == 1  # de-duplicated per hour

    beat_workers(bench)
    report = tick(runtime)
    assert report.gate_open is True
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert status_row(bench).entry_gate_open is True


def test_an_intent_is_not_traded_when_the_gate_closes_before_delivery(bench):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    beat_workers(bench)
    runtime = bench.orchestrator(require_protection_workers=True)
    with session_scope(bench.factory) as session:
        assert take_in(
            session, bench.account, now=bench.now(), lookback=timedelta(hours=2), limit=10
        )
    bench.clock.advance(timedelta(minutes=5))  # both workers stop beating before delivery
    tick(runtime)
    [intent] = bench.intents()
    assert intent.status is IntentStatus.NOT_TRADED
    assert intent.reasons[0]["code"] == "ENTRY_GATE_CLOSED"
    assert {g["code"] for g in intent.reasons[0]["gate"]} >= {"POSITION_MONITOR_DOWN"}
    assert bench.reservations() == []
    assert bench.orders() == []


# ====================================================================== backstops
def test_crashed_workers_are_backstopped_and_exits_pass_every_halt(bench):
    sim = sim_of(bench)
    asset, entry = open_long(bench, sim, "BKS")
    # Everything that halts entries is on: emergency stop (kill switch engaged) and, below,
    # a closed entry gate. None of it may stop the stop-loss exit.
    bench.control.controls.emergency_stop(bench.account, actor=OPERATOR, reason="halt all")
    beat_workers(bench)  # the workers ran ...
    runtime = bench.orchestrator(require_protection_workers=True)
    sim.later(300)  # ... and then crashed: no heartbeat for five minutes
    sim.quote(asset, "94.60", "94.65")  # the bid crossed the stop 94.70
    sim.later(1)
    report = tick(runtime)
    assert report.gate_open is False
    assert report.protection["monitor_backstop"]["exits_submitted"] == 1
    assert "execution_backstop" in report.protection  # the exit reached the paper broker
    [stop_exit] = exits(bench, entry)
    assert stop_exit.exit_reason is ExitReason.STOP_LOSS
    sim.later(2)
    sim.quote(asset, "94.55", "94.60")
    sim.later(1)
    tick(runtime)  # the execution backstop simulates the fill and applies it
    position = sim.position(entry)
    assert position is not None
    assert (position.status, position.exit_reason) == (PositionStatus.CLOSED, ExitReason.STOP_LOSS)
    [fill] = bench.ex.fills(stop_exit.id)
    assert fill.price == D("94.52")  # the bid less slippage: never the stop price
    for event_type in ("POSITION_MONITOR_DOWN", "PAPER_EXECUTION_DOWN"):
        [alert] = bench.health_events(event_type)
        assert alert.severity is Severity.CRITICAL
    with session_scope(bench.factory) as session:
        assert breaker_statuses(session, bench.account)[BreakerType.KILL_SWITCH] is (
            BreakerStatus.TRIPPED
        )
    # The workers come back: nothing is exited twice.
    beat_workers(bench)
    sim.cycle()
    assert len(exits(bench, entry)) == 1
    assert len(bench.ex.fills(stop_exit.id)) == 1
    assert tick(runtime).gate_open is True
    books_reconcile(bench)


def test_a_cleanly_stopped_paper_broker_is_reported_not_restarted(bench):
    sim = sim_of(bench)
    _asset, entry = open_long(bench, sim, "BKT")
    with session_scope(bench.factory) as session:
        bench.ex.broker.shutdown(session, bench.clock.now())
    beat_workers(bench, broker=False)
    report = tick(bench.orchestrator(require_protection_workers=True))
    assert "execution_backstop" not in (report.protection or {})
    [alert] = bench.health_events("PAPER_EXECUTION_STOPPED")
    assert alert.severity is Severity.CRITICAL
    still = sim.position(entry)
    assert still is not None
    assert still.status is PositionStatus.OPEN


# ====================================================================== gap rule
def test_a_watched_stop_crossed_by_a_gap_exits_no_better_than_the_gap(fresh_engine):
    sim = make_simulation(fresh_engine)  # the current execution model: version 2
    ex = sim.ex
    account = ex.account("gap-test")
    asset = ex.asset("GAPX")
    r = ex.reservation(account, asset, "gap-test-reservation")
    entry = ex.open_order(account, r, "10", "gap-test-entry-order")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    ex.dispatch()
    start = SESSION + timedelta(minutes=3)
    # The next bar opened at 90.00, far below the 94.70 stop, and recovered to 94.00.
    sim.bar(asset, start, "90.00", "94.50", "89.50", "94.00", "5000")
    sim.at(start + timedelta(minutes=1, seconds=5))
    summary = sim.monitor_once()
    assert summary.triggers == 1
    assert summary.exits_submitted == 1
    ex.dispatch()
    sim.later(2)
    sim.quote(asset, "93.90", "93.95")  # the market has recovered meanwhile
    sim.later(1)
    sim.simulate()
    ex.dispatch()
    position = sim.position(entry)
    assert position is not None
    assert position.status is PositionStatus.CLOSED
    [stop_exit] = [o for o in exits_of(sim, entry)]
    [fill] = ex.fills(stop_exit.id)
    # Version 1 would fill at the recovered bid (about 93.88); version 2 no better than
    # the gap open less the slippage, and never below the bar's low.
    assert D("89.50") <= fill.price <= D("89.98")
    [detail] = sim.details(stop_exit.id)
    assert any("gap rule" in a for a in detail.assumptions)
    assert ex.risk.reconcile(account) == "PASSED"


def exits_of(sim: Sim, entry: uuid.UUID) -> list[OrderRecord]:
    with session_scope(sim.factory) as session:
        rows = session.scalars(
            select(OrderRecord).where(
                OrderRecord.parent_order_id == entry, OrderRecord.purpose == OrderPurpose.EXIT
            )
        ).all()
        session.expunge_all()
        return list(rows)


# ====================================================================== schedules
def test_daily_jobs_missed_during_an_outage_are_caught_up(bench):
    calls = bench.calls
    research = {"decision_outcomes": calls.stub("outcomes")}

    # Tuesday 2026-11-03 22:30 UTC = 17:30 New York: after the close plus the delays. The
    # research job reads final daily bars (D-230): Tuesday's bar is not final before
    # midnight New York, so the newest session whose data is ready is Monday's.
    runtime = bench.orchestrator(run_decision_outcomes=True, research=research)
    tick(runtime)
    assert calls.counts["outcomes"] == 1
    outcomes = jobs(bench, JobKind.DECISION_OUTCOMES)
    assert outcomes["daily:2026-11-02"].status is JobStatus.SUCCEEDED
    # The days before the first run are recorded as covered, not run two more times.
    assert {s: j.status for s, j in outcomes.items() if s != "daily:2026-11-02"} == {
        "daily:2026-10-29": JobStatus.SUPERSEDED,
        "daily:2026-10-30": JobStatus.SUPERSEDED,
    }
    # Later the same evening nothing more is due; after midnight New York (plus the
    # margin) Tuesday's data is final and its slot runs, once.
    bench.clock.set(datetime(2026, 11, 4, 4, 0, tzinfo=UTC))  # 23:00 New York
    tick(runtime)
    assert calls.counts["outcomes"] == 1
    bench.clock.set(datetime(2026, 11, 4, 5, 20, tzinfo=UTC))  # 00:20 New York
    tick(runtime)
    tick(runtime)
    assert calls.counts["outcomes"] == 2
    assert jobs(bench, JobKind.DECISION_OUTCOMES)["daily:2026-11-03"].status is JobStatus.SUCCEEDED
    reviews = jobs(bench, JobKind.SESSION_CLOSE_REVIEW)
    assert sorted(reviews) == [
        "eod:2026-10-29",
        "eod:2026-10-30",
        "eod:2026-11-02",
        "eod:2026-11-03",
    ]
    assert reviews["eod:2026-11-03"].result["day"] == "2026-11-03"

    # The orchestrator is down Wednesday to Friday; it restarts Monday before the close.
    bench.clock.set(datetime(2026, 11, 9, 16, 0, tzinfo=UTC))
    restarted = bench.orchestrator(
        run_decision_outcomes=True, research=research, worker_id="orchestrator:restarted"
    )
    report = tick(restarted)
    assert calls.counts["outcomes"] == 3  # once, for the newest session (Friday's)
    outcomes = jobs(bench, JobKind.DECISION_OUTCOMES)
    assert outcomes["daily:2026-11-06"].status is JobStatus.SUCCEEDED
    assert outcomes["daily:2026-11-04"].status is JobStatus.SUPERSEDED
    assert outcomes["daily:2026-11-05"].status is JobStatus.SUPERSEDED
    reviews = jobs(bench, JobKind.SESSION_CLOSE_REVIEW)
    for day in ("2026-11-04", "2026-11-05", "2026-11-06"):  # each missed day on its own
        assert reviews[f"eod:{day}"].status is JobStatus.SUCCEEDED
        assert reviews[f"eod:{day}"].result["day"] == day
    assert any(j.caught_up for j in report.jobs)
    tick(restarted)
    assert calls.counts["outcomes"] == 3  # nothing runs twice


def test_interval_gaps_are_reported_and_not_back_filled(bench):
    runtime = bench.orchestrator()
    tick(runtime)
    bench.clock.advance(timedelta(hours=2))
    tick(runtime)
    gaps = {g.subject_id: g for g in bench.health_events("ORCHESTRATION_SCHEDULE_GAP")}
    assert gaps["RISK_MONITOR"].details["missed"] == 23  # 2 h of 5-minute slots, minus one
    assert gaps["RISK_MONITOR"].severity is Severity.WARNING
    assert len(jobs(bench, JobKind.RISK_MONITOR)) == 2


def test_a_job_kind_never_runs_in_two_slots_at_once(bench):
    first, second = "2026-11-03T22:30:00Z", "2026-11-03T22:35:00Z"
    a, b = runner(bench, "worker-a"), runner(bench, "worker-b")
    claimed = a.claim(JobKind.RISK_MONITOR, first)
    assert claimed is not None
    bench.clock.advance(timedelta(minutes=6))  # the next slot began; slot 1 still runs
    assert b.claim(JobKind.RISK_MONITOR, second) is None
    a._finish(claimed[0], {"fixture": True}, None)
    assert b.claim(JobKind.RISK_MONITOR, second) is not None


def test_a_long_job_renews_its_lease_and_is_not_reclaimed(bench):
    slot = "2026-11-03T22:30:00Z"
    lease = timedelta(seconds=3)
    a, b = runner(bench, "long-runner", lease=lease), runner(bench, "other", lease=lease)
    seen: dict[str, Any] = {}

    def body() -> dict[str, Any]:
        bench.clock.advance(timedelta(seconds=2))
        time.sleep(1.6)  # the keeper renews (every lease / 3 = 1 s)
        bench.clock.advance(timedelta(seconds=2))  # 4 s after the claim: past the first lease
        seen["other"] = b.claim(JobKind.RISK_MONITOR, slot)
        return {"fixture": True}

    [outcome] = a.run_due([JobSpec(JobKind.RISK_MONITOR, lambda _at: slot, body)])
    assert seen["other"] is None
    assert outcome.status is JobStatus.SUCCEEDED
    row = jobs(bench, JobKind.RISK_MONITOR)[slot]
    assert (row.attempts, row.reclaimed) == (1, 0)
    assert bench.health_events("ORCHESTRATION_JOB_STUCK") == []


def test_failed_slots_are_superseded_and_dead_letters_requeued_by_an_operator(bench):
    calls = {"n": 0}

    def flaky() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("TEST FIXTURE failure")
        return {"fixture": True}

    spec = JobSpec(JobKind.RISK_MONITOR, every(300), flaky, interval_seconds=300)
    retrying = runner(bench, "retrying")
    [failed] = retrying.run_due([spec])
    assert failed.status is JobStatus.FAILED
    bench.clock.advance(timedelta(minutes=5))
    [ok] = retrying.run_due([spec])
    assert ok.status is JobStatus.SUCCEEDED
    assert jobs(bench, JobKind.RISK_MONITOR)[failed.slot].status is JobStatus.SUPERSEDED

    calls["n"] = 0
    once = runner(bench, "once", max_attempts=1)
    dead_spec = JobSpec(JobKind.ACCOUNTING_RECONCILE, every(3600), flaky, interval_seconds=3600)
    [dead] = once.run_due([dead_spec])
    assert dead.status is JobStatus.DEAD
    assert bench.health_events("ORCHESTRATION_JOB_DEAD")
    row = jobs(bench, JobKind.ACCOUNTING_RECONCILE)[dead.slot]
    with pytest.raises(JobControlError):
        once.requeue(row.id, actor="system:auto", reason="not an operator")
    requeued = once.requeue(row.id, actor=OPERATOR, reason="cause fixed (fixture)")
    assert (requeued.status, requeued.attempts, requeued.requeued_by) == (
        JobStatus.FAILED,
        0,
        OPERATOR,
    )
    [again] = once.run_due([dead_spec])
    assert again.status is JobStatus.SUCCEEDED
    final = jobs(bench, JobKind.ACCOUNTING_RECONCILE)[dead.slot]
    assert final.result["requeued_from"][0]["attempts"] == 1
    assert final.result["requeued_from"][0]["by"] == OPERATOR
    with pytest.raises(JobControlError):
        once.requeue(row.id, actor=OPERATOR, reason="not dead any more")
    with session_scope(bench.factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "orchestration.job.requeue")
            )
            == 1
        )
    assert bench.health_events("ORCHESTRATION_JOB_REQUEUED")


# ====================================================================== feeds and data
def test_a_feed_outage_halts_entries_and_invents_no_exit_until_data_returns(bench):
    sim = sim_of(bench)
    asset, entry = open_long(bench, sim, "FDS")
    runtime = bench.orchestrator(require_protection_workers=True)
    # No observation for 40 minutes (the intraday freshness limit is 30).
    sim.later(40 * 60)
    beat_workers(bench)
    sim.monitor_once()
    report = tick(runtime)
    [stale] = bench.health_events("FEED_STALE")
    assert stale.severity is Severity.HIGH
    freshness = next(j for j in report.jobs if j.kind is JobKind.DATA_FRESHNESS)
    assert freshness.status is JobStatus.SUCCEEDED
    row = next(iter(jobs(bench, JobKind.DATA_FRESHNESS).values()))
    assert [s["symbol"] for s in row.result["stale"]] == ["FDS"]
    with session_scope(bench.factory) as session:
        assert breaker_statuses(session, bench.account)[BreakerType.DATA_QUALITY] is (
            BreakerStatus.TRIPPED
        )
    position = sim.position(entry)
    assert position is not None
    assert position.monitor_status == "STALE"
    assert exits(bench, entry) == []  # no exit at a stale or invented price
    # The recovery backfill delivers a bar from the outage that crossed the stop: the stop
    # triggers now, from that real observation.
    sim.bar(asset, SESSION + timedelta(minutes=20), "95.00", "95.10", "94.00", "94.20", "5000")
    summary = sim.monitor_once()
    assert summary.triggers == 1
    bench.ex.dispatch()
    sim.later(2)
    sim.quote(asset, "94.30", "94.35")
    sim.later(1)
    sim.cycle()
    closed = sim.position(entry)
    assert closed is not None
    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason is ExitReason.STOP_LOSS
    beat_workers(bench)
    bench.clock.advance(timedelta(minutes=5))
    beat_workers(bench)
    tick(runtime)  # the next risk-monitor slot: marks are complete again
    with session_scope(bench.factory) as session:
        assert breaker_statuses(session, bench.account)[BreakerType.DATA_QUALITY] is (
            BreakerStatus.CLEAR
        )
    books_reconcile(bench)


# ====================================================================== processes
class _FlakyMonitor:
    def __init__(self, failures: int) -> None:
        self.left = failures

    def run_once(self) -> MonitorSummary:
        if self.left > 0:
            self.left -= 1
            raise OperationalError("SELECT 1", {}, ConnectionError("server closed (FIXTURE)"))
        return MonitorSummary(positions=0)


def test_the_position_monitor_survives_a_database_outage_and_reports_its_heartbeat(bench):
    writer = HeartbeatWriter(
        bench.factory, bench.clock, POSITION_MONITOR, interval_seconds=5, instance="fixture-1"
    )
    ctx: Any = SimpleNamespace(
        monitor=_FlakyMonitor(2), session_factory=bench.factory, clock=bench.clock
    )
    stop = threading.Event()
    assert position_monitor.run(ctx, stop, interval=0.0, heartbeat=writer, max_passes=4) == 0
    with session_scope(bench.factory) as session:
        row = heartbeats.latest(session, POSITION_MONITOR)
        assert row is not None
        assert (row.state, row.passes) == (WorkerState.STOPPED, 4)
        assert "OperationalError" in (row.last_error or "")
    # A heartbeat whose database is unreachable never raises; the worker goes on.
    dead = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    try:
        broken = HeartbeatWriter(
            sessionmaker(dead), bench.clock, POSITION_MONITOR, interval_seconds=5
        )
        assert broken.beat(passes=1) is False
        assert broken.failures == 1
    finally:
        dead.dispose()
    # The workers report reads the heartbeat (before 10B the monitor's liveness was unknown).
    bench.clock.advance(timedelta(seconds=1))  # newer than the stopped instance's row
    beat = HeartbeatWriter(
        bench.factory, bench.clock, POSITION_MONITOR, interval_seconds=5, instance="fixture-2"
    )
    beat.beat(passes=1)
    switches = Switches(*([None] * 11))
    with session_scope(bench.factory) as session:
        report = workers_report(session, bench.now(), switches)
        monitor = next(w for w in report.workers if w.worker == POSITION_MONITOR)
        assert (monitor.observation.value, monitor.health) == ("HEARTBEAT", WorkerHealth.RUNNING)
        later = workers_report(session, bench.now() + timedelta(minutes=2), switches)
        stale = next(w for w in later.workers if w.worker == POSITION_MONITOR)
        assert stale.health is WorkerHealth.STALE


def test_database_connections_terminated_mid_run_lose_nothing(bench, fresh_engine):
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    runtime = bench.orchestrator()
    tick(runtime)
    second = bench.decision(bench.asset("ORCB"), symbol="ORCB")
    assert terminate_connections(fresh_engine) >= 1  # the application's pooled connections
    bench.clock.advance(timedelta(seconds=30))
    outcomes = []
    for _ in range(3):  # the first tick after the interruption may fail; the next recover
        try:
            outcomes.append(tick(runtime))
        except OperationalError:
            outcomes.append(None)
        bench.clock.advance(timedelta(seconds=30))
    intents = {i.ai_decision_id: i for i in bench.intents()}
    assert intents[second].status is IntentStatus.ORDER_CREATED
    assert len(bench.orders()) == 2
    assert len(bench.reservations()) == 2


def test_orchestration_does_not_depend_on_redis(bench, monkeypatch):
    def down(*_a: Any, **_k: Any) -> Any:
        raise redis.exceptions.ConnectionError("TEST FIXTURE: Redis is down")

    monkeypatch.setattr(redis.Redis, "execute_command", down)
    asset = bench.asset("ORCA")
    bench.decision(asset)
    bench.enable()
    report = tick(bench.orchestrator())
    assert report.errors == []
    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED


# ====================================================================== soak
def test_sustained_operation_with_restarts_outages_and_duplicate_deliveries(bench, fresh_engine):
    """Ninety ticks (45 simulated minutes of a regular session) with new decisions, moving
    MOCK quotes, stops being hit, worker crashes (backstopped), orchestrator restarts,
    terminated database connections, a second orchestrator ticking alongside and
    re-delivered intent events. Then every invariant: no duplicate intent, reservation,
    order, fill or ledger entry, and every reconciliation passes."""
    sim = sim_of(bench)
    symbols = ["SKA", "SKB", "SKC"]
    assets = {s: bench.asset(s) for s in symbols}
    sim.at(SESSION + timedelta(minutes=1))
    bench.enable()
    beat_workers(bench)
    runtime = bench.orchestrator(require_protection_workers=True, worker_id="soak-1")
    rival = bench.orchestrator(require_protection_workers=True, worker_id="soak-rival")
    crashed = set(range(30, 42))  # both protection workers down: backstopped
    restarts = {20, 60}
    interruptions = {50, 75}
    decisions: list[uuid.UUID] = []
    failures = duplicates = 0
    for i in range(90):
        now = sim.later(30)
        for k, symbol in enumerate(symbols):
            # Deterministic MOCK paths: SKB falls through its stop after tick 45.
            base = D("99.50") + D(k) * D("0.01") + D(i % 5) * D("0.02")
            if symbol == "SKB" and i >= 45:
                base = D("94.00")
            sim.quote(assets[symbol], str(base), str(base + D("0.05")), at=now)
        if i % 10 == 0 and i < 70:
            symbol = symbols[(i // 10) % len(symbols)]
            decisions.append(
                bench.decision(assets[symbol], symbol=symbol, age=timedelta(seconds=30))
            )
        sim.later(1)
        if i not in crashed:
            beat_workers(bench)
            sim.cycle(1)
        if i in restarts:
            runtime = bench.orchestrator(require_protection_workers=True, worker_id=f"soak-{i}")
        if i in interruptions:
            terminate_connections(fresh_engine)
        if i == 25:
            # Duplicate deliveries: a second event for every intent handled so far (a
            # delivered event itself is final in the database, so the duplicate is new).
            with session_scope(bench.factory) as session:
                for intent_id in session.scalars(
                    select(TradeIntentRecord.id).where(
                        TradeIntentRecord.status != IntentStatus.RECEIVED
                    )
                ).all():
                    enqueue(
                        session,
                        topic=INTENT_TOPIC,
                        aggregate_type="trade_intent",
                        aggregate_id=intent_id,
                        dedupe_key=f"duplicate-delivery:{intent_id}",
                        payload={"intent_id": str(intent_id)},
                        at=now,
                    )
                    duplicates += 1
        for orchestrator in (runtime, rival) if i % 7 == 0 else (runtime,):
            try:
                tick(orchestrator)
            except OperationalError:
                failures += 1  # a terminated connection; the next tick recovers
    beat_workers(bench)
    sim.cycle()
    tick(runtime)

    with session_scope(bench.factory) as session:

        def count(model: Any, *where: Any) -> int:
            return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

        intents = session.scalars(select(TradeIntentRecord)).all()
        assert len(intents) == len({i.ai_decision_id for i in intents}) <= len(decisions)
        assert all(i.status is not IntentStatus.RECEIVED for i in intents)
        ordered = [i for i in intents if i.status is IntentStatus.ORDER_CREATED]
        assert ordered, "the soak must actually trade"
        # One entry order and one reservation per decision, never two.
        entry_orders = session.scalars(
            select(OrderRecord).where(OrderRecord.purpose == OrderPurpose.ENTRY)
        ).all()
        decisions_with_orders = [o.ai_decision_id for o in entry_orders if o.ai_decision_id]
        assert len(decisions_with_orders) == len(set(decisions_with_orders))
        reserved = [i.reservation_id for i in intents if i.reservation_id is not None]
        assert len(reserved) == len(set(reserved))
        # Every simulated fill event became exactly one fill row.
        fill_events = count(
            PaperBrokerEventRecord, PaperBrokerEventRecord.event_type == BrokerEventType.FILL
        )
        assert fill_events == count(OrderFillRecord)
        # Positions: never more closed than bought; closed means flat.
        for p in session.scalars(select(PaperPositionRecord)).all():
            assert p.open_quantity >= 0
            if p.status is PositionStatus.CLOSED:
                assert p.open_quantity == 0
        stopped = session.scalars(
            select(PaperPositionRecord).where(
                PaperPositionRecord.exit_reason == ExitReason.STOP_LOSS
            )
        ).all()
        assert stopped, "SKB's stop must have been hit and exited"
        # One protective exit per triggered position.
        for p in stopped:
            protective = count(
                OrderRecord,
                OrderRecord.parent_order_id == p.entry_order_id,
                OrderRecord.exit_reason == ExitReason.STOP_LOSS,
            )
            assert protective == 1
    assert bench.health_events("POSITION_MONITOR_DOWN")  # the crash window was seen
    assert bench.health_events("PAPER_EXECUTION_DOWN")
    assert failures <= len(interruptions)
    assert duplicates > 0  # the duplicate deliveries happened and changed nothing above
    with session_scope(bench.factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(
                    OutboxEventRecord.topic == INTENT_TOPIC,
                    OutboxEventRecord.status != OutboxStatus.DELIVERED,
                )
            )
            == 0
        )
    books_reconcile(bench)


def test_the_session_review_records_one_valuation_checkpoint_for_the_day(bench):
    """D-232: every session gets a valuation checkpoint, flat or not, so the Performance
    page's daily returns include flat days. Caught-up past days get none (no backdating)."""
    runtime = bench.orchestrator()  # Tuesday 2026-11-03 22:30 UTC, after the review time
    tick(runtime)
    reviews = jobs(bench, JobKind.SESSION_CLOSE_REVIEW)
    today = reviews["eod:2026-11-03"].result["valuation_checkpoint"]
    assert today is not None
    assert (today["status"], today["created"]) == ("COMPLETE", True)
    assert D(today["equity"]) == D("100000")  # the flat CONTROLLED_TEST account
    for day in ("2026-10-29", "2026-10-30", "2026-11-02"):  # caught up: no backdated value
        assert reviews[f"eod:{day}"].result["valuation_checkpoint"] is None
    tick(runtime)
    with session_scope(bench.factory) as session:
        snapshots = session.scalar(
            select(func.count())
            .select_from(PortfolioSnapshotRecord)
            .where(PortfolioSnapshotRecord.portfolio_account_id == bench.account)
        )
    assert snapshots == 1  # once per day, however often the orchestrator ticks

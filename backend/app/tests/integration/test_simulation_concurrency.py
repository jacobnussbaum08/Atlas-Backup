"""Phase 7B concurrency, restart and interruption tests against a real PostgreSQL server.

Many threads, each with its own pooled connection, released together by a barrier, run the
execution simulator and the position monitor over the same orders and positions. Afterwards
the database is checked against independent counts: each observation's liquidity produced
at most one fill, every fill was posted once to the 6A ledger and consumed once from its
6C reservation, every position got one protective exit, and every reconciliation (execution
with positions, 6C reservations, 6A books) passes. Crashes and lost connections inside an
evaluation leave nothing behind and the retry fills exactly once.

TEST FIXTURE market data; simulated paper orders and fills only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import OperationalError

from app.database.models.accounting import JournalEntryRecord
from app.database.models.execution import (
    ExecutionFillDetailRecord,
    OrderFillRecord,
    OrderRecord,
    PaperBrokerEventRecord,
)
from app.database.sessions import session_scope
from app.execution.enums import ExitReason, OrderPurpose
from app.execution.reconciliation import ExecutionReconciler
from app.execution.simulation import ExecutionSimulator
from app.tests.integration.simulation_support import SESSION, D, Sim, make_simulation

pytestmark = pytest.mark.integration


@pytest.fixture
def wide_engine(fresh_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(
        fresh_engine.url.render_as_string(hide_password=False),
        pool_size=40,
        max_overflow=0,
        pool_timeout=120,
        connect_args={"options": "-c timezone=UTC"},
    )
    yield engine
    engine.dispose()


@pytest.fixture
def sim(wide_engine: Engine) -> Sim:
    return make_simulation(wide_engine)


def run_threads(count: int, work: Callable[[int], Any]) -> tuple[list[Any], list[BaseException]]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count
    errors: list[BaseException] = []
    lock = threading.Lock()

    def target(i: int) -> None:
        try:
            barrier.wait()
            results[i] = work(i)
        except BaseException as exc:  # collected and asserted on
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return results, errors


def count(sim: Sim, model: Any, *where: Any) -> int:
    with session_scope(sim.factory) as session:
        return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def all_reconciled(sim: Sim, accounts: list[Any]) -> None:
    sim.ex.dispatch()
    result = ExecutionReconciler(sim.factory, sim.clock).run()
    assert result.passed, result.findings
    for account in accounts:
        assert sim.ex.risk.reconcile(account) == "PASSED"


def test_racing_simulators_use_each_quote_once(sim):
    ex = sim.ex
    account = ex.account("cc-quote")
    asset = ex.asset("CCQ")
    r = ex.reservation(account, asset, "cc-res-quote")
    order = ex.open_order(account, r, "30", "cc-order-quote")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50), ask_size="20")
    _results, errors = run_threads(12, lambda _i: sim.simulator.evaluate_order(order, force=True))
    assert errors == []
    assert [f.quantity for f in ex.fills(order)] == [D(10)]  # 50% of 20 displayed, once
    sim.quote(asset, "99.51", "99.56", ask_size="20")
    sim.later(1)
    _results, errors = run_threads(12, lambda _i: sim.simulator.run_once())
    assert errors == []
    assert [f.quantity for f in ex.fills(order)] == [D(10), D(10)]
    assert ex.rv.get(r).consumed_quantity == D(20)
    assert sim.position(order).open_quantity == D(20)
    all_reconciled(sim, [account])


def test_many_workers_fill_many_orders_exactly_once(sim):
    ex = sim.ex
    accounts, orders = [], []
    for n in range(4):
        account = ex.account(f"cc-many-{n}")
        accounts.append(account)
        for k in range(3):
            asset = ex.asset(f"CM{n}{k}")
            r = ex.reservation(account, asset, f"cc-res-{n}-{k}")
            orders.append((ex.open_order(account, r, "10", f"cc-order-{n}-{k}"), asset))
    sim.at(SESSION + timedelta(minutes=1))
    for _order, asset in orders:
        sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    _results, errors = run_threads(8, lambda _i: sim.simulator.run_once())
    assert errors == []
    for order, _asset in orders:
        assert ex.order(order).status.value == "FILLED"
        assert len(ex.fills(order)) == 1
    assert count(sim, OrderFillRecord) == len(orders)
    assert count(sim, ExecutionFillDetailRecord) == len(orders)
    assert count(
        sim, JournalEntryRecord, JournalEntryRecord.source == "SIMULATED_EXECUTION"
    ) == len(orders)
    all_reconciled(sim, accounts)


def test_racing_monitors_submit_one_protective_exit_per_position(sim):
    ex = sim.ex
    account = ex.account("cc-monitor")
    entries = []
    for k in range(3):
        asset = ex.asset(f"CCM{k}")
        r = ex.reservation(account, asset, f"cc-mres-{k}")
        entries.append((ex.open_order(account, r, "10", f"cc-morder-{k}"), asset))
    sim.at(SESSION + timedelta(minutes=1))
    for _entry, asset in entries:
        sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    sim.later(30)
    for _entry, asset in entries:
        sim.quote(asset, "94.60", "94.65")
    _results, errors = run_threads(8, lambda _i: sim.monitor.run_once())
    assert errors == []
    for entry, _asset in entries:
        exits = count(
            sim,
            OrderRecord,
            OrderRecord.parent_order_id == entry,
            OrderRecord.purpose == OrderPurpose.EXIT,
        )
        assert exits == 1
        assert sim.position(entry).trigger_reason is ExitReason.STOP_LOSS
    # Workers keep running concurrently until everything is closed.
    ex.dispatch()
    sim.later(2)
    for _entry, asset in entries:
        sim.quote(asset, "94.55", "94.60")
    sim.later(1)
    _results, errors = run_threads(
        6, lambda i: sim.simulator.run_once() if i % 2 else sim.monitor.run_once()
    )
    assert errors == []
    sim.cycle()
    for entry, _asset in entries:
        position = sim.position(entry)
        assert position.status.value == "CLOSED"
        assert position.quantity_closed == D(10)
    all_reconciled(sim, [account])


def test_a_crash_inside_an_evaluation_leaves_nothing_and_the_retry_fills_once(sim, monkeypatch):
    ex = sim.ex
    account = ex.account("cc-crash")
    asset = ex.asset("CCX")
    r = ex.reservation(account, asset, "cc-res-crash")
    order = ex.open_order(account, r, "10", "cc-order-crash")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    original = ex.service.positions.on_fill
    calls = {"n": 0}

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated worker crash after the ledger posting")
        return original(*args, **kwargs)

    monkeypatch.setattr(ex.service.positions, "on_fill", crash_once)
    summary = sim.simulate()
    assert len(summary.errors) == 1
    # Rolled back as one unit: no broker event, no fill, no posting, cursors unchanged.
    assert count(sim, PaperBrokerEventRecord, PaperBrokerEventRecord.event_type == "FILL") == 0
    assert count(sim, OrderFillRecord) == 0
    assert count(sim, JournalEntryRecord, JournalEntryRecord.source == "SIMULATED_EXECUTION") == 0
    assert sim.book(order).evaluations == 0
    # A restarted simulator (a new instance) evaluates again and fills once.
    restarted = ExecutionSimulator(ex.service, sim.clock, sim.simulator.model)
    assert restarted.run_once().fills == 1
    assert restarted.run_once().fills == 0
    assert len(ex.fills(order)) == 1
    all_reconciled(sim, [account])


def test_a_lost_database_connection_is_retried_without_a_duplicate_fill(sim, monkeypatch):
    ex = sim.ex
    account = ex.account("cc-conn")
    asset = ex.asset("CCD")
    r = ex.reservation(account, asset, "cc-res-conn")
    order = ex.open_order(account, r, "10", "cc-order-conn")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    ledger = ex.service._ledger
    original = ledger.post_fill
    calls = {"n": 0}

    def drop_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, Exception("server closed the connection"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "post_fill", drop_once)
    first = sim.simulate()
    assert len(first.errors) == 1
    assert first.fills == 0
    assert count(sim, OrderFillRecord) == 0
    second = sim.simulate()
    assert second.fills == 1
    assert len(ex.fills(order)) == 1
    all_reconciled(sim, [account])


def test_a_paper_broker_restart_resumes_without_duplicates(sim):
    ex = sim.ex
    account = ex.account("cc-restart")
    asset = ex.asset("CCR")
    r = ex.reservation(account, asset, "cc-res-restart")
    order = ex.open_order(account, r, "30", "cc-order-restart")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50), ask_size="20")
    sim.simulate()
    with session_scope(sim.factory) as session:
        ex.broker.shutdown(session, sim.clock.now())
    sim.quote(asset, "99.51", "99.56", ask_size="20")
    sim.later(1)
    stopped = sim.simulate()  # a stopped broker decides nothing
    assert (stopped.paused, stopped.evaluated, stopped.fills) == (True, 0, 0)
    assert len(ex.fills(order)) == 1
    report = ex.service.recover()  # restart: broker RUNNING, lost deliveries applied
    assert report.errors == []
    sim.later(1)
    resumed = sim.simulate()
    assert resumed.fills == 1
    assert [f.quantity for f in ex.fills(order)] == [D(10), D(10)]
    all_reconciled(sim, [account])

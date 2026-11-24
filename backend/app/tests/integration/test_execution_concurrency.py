"""Phase 7A concurrency tests against a real PostgreSQL server.

Many threads, each with its own pooled connection, released together by a barrier. After
each run the database is checked against independent counts: one order per reservation,
one broker book per order, every fill applied once, quantities conserved between the
order, the reservation and the ledger, and every reconciliation (execution, 6C
reservations, 6A books) passes. TEST FIXTURE data; simulated paper orders only.
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select

from app.accounting.posting import load_state
from app.database.models.execution import (
    ExecutionRequestRecord,
    OrderEventRecord,
    OrderFillRecord,
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerOrderRecord,
)
from app.database.sessions import session_scope
from app.execution.enums import CancelMode, ExecutionDecision, OrderStatus, OutboxStatus
from app.execution.reconciliation import ExecutionReconciler
from app.execution.service import ExecutionError
from app.paper_broker.interface import BrokerRefusedError
from app.risk.enums import ReservationStatus
from app.tests.integration.execution_support import Execution, make_execution

pytestmark = pytest.mark.integration
S = OrderStatus


@pytest.fixture
def wide_engine(fresh_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(
        fresh_engine.url.render_as_string(hide_password=False),
        pool_size=48,
        max_overflow=0,
        pool_timeout=120,
        connect_args={"options": "-c timezone=UTC"},
    )
    yield engine
    engine.dispose()


@pytest.fixture
def ex(wide_engine: Engine) -> Execution:
    return make_execution(wide_engine)


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


def everything_reconciles(ex: Execution, account: uuid.UUID) -> None:
    outcome = ExecutionReconciler(ex.factory, ex.risk.clock).run(account)
    assert outcome.findings == [], outcome.findings
    assert ex.rv.reconciler.run(account, actor="system:test").status.value == "PASSED"
    assert ex.risk.reconcile(account) == "PASSED"


def test_the_same_submission_from_twenty_workers_creates_one_order(ex):
    account = ex.account()
    r = ex.reservation(account, ex.asset("CSA"), "csa-res-0001")
    request = ex.entry(account, r, "50")
    results, errors = run_threads(20, lambda i: ex.submit(request, "csa-order-0001"))
    assert errors == []
    assert len({res.order_id for res in results}) == 1
    assert Counter(res.created for res in results) == {True: 1, False: 19}
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord) == 1
        assert ex.count(session, ExecutionRequestRecord) == 1
    assert [e.event_type.value for e in ex.rv.events(r)].count("ORDER_ATTACHED") == 1


def test_competing_keys_for_one_reservation_attach_exactly_one_order(ex):
    account = ex.account()
    r = ex.reservation(account, ex.asset("CSB"), "csb-res-0001")
    results, errors = run_threads(
        20, lambda i: ex.submit(ex.entry(account, r, "50"), f"csb-order-{i:04d}")
    )
    assert errors == []
    created = [res for res in results if res.decision is ExecutionDecision.ORDER_CREATED]
    refused = [res for res in results if res.decision is ExecutionDecision.REFUSED]
    assert len(created) == 1
    assert len(refused) == 19
    assert all("DUPLICATE_ORDER" in {x["code"] for x in res.reasons} for res in refused)
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord) == 1
    reservation = ex.rv.get(r)
    assert reservation.order_ref == str(created[0].order_id)


def test_concurrent_orders_for_many_reservations_and_competing_dispatchers(ex):
    account = ex.risk.account(configure=False)
    ex.risk.configure(account, max_open_positions=50)
    ex.risk.reconcile(account)
    reservations = [
        ex.reservation(account, ex.asset(f"CM{i:02d}"), f"cm-res-{i:04d}", requested_quantity=D(10))
        for i in range(12)
    ]
    # 36 submissions: each reservation sent three times under one key.
    results, errors = run_threads(
        36,
        lambda i: ex.submit(
            ex.entry(account, reservations[i % 12], "10"), f"cm-order-{i % 12:04d}"
        ),
    )
    assert errors == []
    assert len({res.order_id for res in results}) == 12
    # Eight dispatcher workers race over the outbox.
    dispatched, errors = run_threads(8, lambda i: ex.service.dispatcher().dispatch(limit=100))
    assert errors == []
    assert sum(d.failed + d.dead for d in dispatched) == 0
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord, OrderRecord.status == S.OPEN) == 12
        assert ex.count(session, PaperBrokerOrderRecord) == 12
        accepted = ex.count(session, OrderEventRecord, OrderEventRecord.event_type == "ACCEPTED")
        assert accepted == 12
        assert (
            ex.count(session, OutboxEventRecord, OutboxEventRecord.status != OutboxStatus.DELIVERED)
            == 0
        )
    everything_reconciles(ex, account)


def test_concurrent_fills_duplicates_and_a_cancellation_conserve_quantity(wide_engine):
    ex = make_execution(wide_engine, cancel_mode=CancelMode.IMMEDIATE)
    account = ex.account()
    asset = ex.asset("CFL")
    r = ex.reservation(account, asset, "cfl-res-0001")
    oid = ex.open_order(account, r, "92", "cfl-order-0001")

    def work(i: int) -> Any:
        if i == 30:
            return ex.cancel(oid, "cfl-cancel-0001")
        # 15 distinct fills of 3 shares, each sent twice (duplicate controlled events).
        return ex.fill(oid, "3", "99.50", f"cfl-fill-{i % 15:04d}")

    _results, errors = run_threads(31, work)
    allowed = (ExecutionError, BrokerRefusedError)  # fills arriving after the cancellation
    assert all(isinstance(e, allowed) for e in errors), errors
    ex.dispatch()
    o = ex.order(oid)
    assert o.status in (S.CANCELLED, S.PARTIALLY_FILLED, S.CANCEL_PENDING, S.FILLED)
    if o.status is not S.CANCELLED:  # the cancellation's confirmation may still be queued
        ex.dispatch()
        o = ex.order(oid)
    assert o.status is S.CANCELLED
    fills = ex.fills(oid)
    assert len(fills) == len({f.broker_event_id for f in fills})  # each applied once
    assert o.filled_quantity == sum((f.quantity for f in fills), D(0)) == 3 * len(fills)
    reservation = ex.rv.get(r)
    assert reservation.status is ReservationStatus.RELEASED
    assert reservation.consumed_quantity == o.filled_quantity
    assert reservation.consumed_quantity + reservation.released_quantity == 92
    with session_scope(ex.factory) as session:
        held = load_state(session, account, "USD").position(asset)
        assert (D(0) if held is None else held.quantity) == o.filled_quantity
        assert (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(OrderFillRecord)
                    .where(OrderFillRecord.order_id == oid)
                )
                or 0
            )
            <= 15
        )
    everything_reconciles(ex, account)


def test_one_broker_event_delivered_by_many_workers_is_applied_once(ex):
    account = ex.account()
    r = ex.reservation(account, ex.asset("CDE"), "cde-res-0001")
    oid = ex.open_order(account, r, "40", "cde-order-0001")
    for i in range(4):
        ex.fill(oid, "10", "99.40", f"cde-fill-{i:04d}", apply=False)

    def deliver(i: int) -> int:
        return int(ex.service.apply_broker_events(oid))

    applied, errors = run_threads(16, deliver)
    assert errors == []
    assert sum(applied) == 4  # exactly one worker applied the four events
    o = ex.order(oid)
    assert (o.status, o.filled_quantity) == (S.FILLED, 40)
    reservation = ex.rv.get(r)  # 40 of 92 consumed once; the 52 never ordered released
    assert (reservation.status, reservation.consumed_quantity) == (ReservationStatus.RELEASED, 40)
    assert reservation.released_quantity == 52
    ex.dispatch()
    everything_reconciles(ex, account)

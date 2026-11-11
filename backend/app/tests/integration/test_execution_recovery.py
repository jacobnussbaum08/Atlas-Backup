"""Phase 7A transactional outbox, retries, crashes, restarts and recovery (real PostgreSQL).

TEST FIXTURE data; simulated paper orders; controlled test execution events only.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.database.models.execution import (
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerEventRecord,
    PaperBrokerOrderRecord,
)
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.events.outbox import OutboxMessage, enqueue
from app.execution.enums import BrokerRunState, ExecutionDecision, OrderStatus, OutboxStatus
from app.execution.portfolio import AccountingPositionLedger
from app.execution.reconciliation import ExecutionReconciler
from app.execution.service import BROKER_TOPIC, ExecutionService
from app.paper_broker.controlled import ControlledPaperBroker
from app.risk.enums import BreakerStatus, BreakerType, ExecutionLink, ReservationStatus
from app.risk.state import breaker_statuses
from app.tests.integration.execution_support import Execution, make_execution

pytestmark = pytest.mark.integration
S = OrderStatus


class SimulatedCrash(BaseException):  # escapes every `except Exception`
    """A process dying mid-transaction (not an application error)."""


@pytest.fixture
def ex(fresh_engine) -> Execution:
    return make_execution(fresh_engine, outbox_max_attempts=3)


def outbox(ex: Execution, topic: str | None = None) -> list[OutboxEventRecord]:
    with session_scope(ex.factory) as session:
        q = select(OutboxEventRecord).order_by(OutboxEventRecord.id)
        if topic is not None:
            q = q.where(OutboxEventRecord.topic == topic)
        rows = list(session.scalars(q))
        session.expunge_all()
        return rows


def books(ex: Execution) -> int:
    with session_scope(ex.factory) as session:
        return ex.count(session, PaperBrokerOrderRecord)


def restarted(ex: Execution) -> ExecutionService:
    """A brand-new engine (as after a process restart) on the same database."""
    return ExecutionService(
        ex.factory,
        ex.risk.clock,
        ex.rv.service._accounting,
        broker=ControlledPaperBroker(),
        reservations=ex.rv.service,
        ledger=AccountingPositionLedger(ex.risk.books.ctx.posting),
        config=ex.service.config,
    )


def submitted(ex: Execution, symbol: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    account = ex.account()
    r = ex.reservation(account, ex.asset(symbol), f"{symbol.lower()}-res-0001")
    result = ex.submit(ex.entry(account, r, "20"), f"{symbol.lower()}-order-0001")
    assert result.decision is ExecutionDecision.ORDER_CREATED
    assert result.order_id is not None
    return account, r, result.order_id


def test_failed_deliveries_are_retried_with_backoff_and_deliver_once(ex, monkeypatch):
    account, _r, oid = submitted(ex, "RTA")
    real = ex.broker.submit_order
    calls = {"n": 0}

    def flaky(session, order, at):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("simulated transient failure")
        return real(session, order, at)

    monkeypatch.setattr(ex.broker, "submit_order", flaky)
    first = ex.dispatcher.dispatch_once()
    assert first is not None
    assert first.status is OutboxStatus.PENDING
    assert "simulated transient failure" in (first.error or "")
    assert ex.dispatcher.dispatch_once() is None  # backing off: not due yet
    ex.risk.clock.advance(timedelta(seconds=5))
    assert ex.dispatcher.dispatch_once().status is OutboxStatus.PENDING
    ex.risk.clock.advance(timedelta(seconds=10))
    assert ex.dispatcher.dispatch_once().status is OutboxStatus.DELIVERED
    (event,) = outbox(ex, "order.submit")
    assert (event.status, event.attempts) == (OutboxStatus.DELIVERED, 3)
    assert ex.order(oid).status is S.OPEN
    assert books(ex) == 1  # retries never duplicated the order at the broker
    assert ExecutionReconciler(ex.factory, ex.risk.clock).run(account).passed


def test_a_dead_submission_is_a_confirmed_failure(ex):
    account, r, oid = submitted(ex, "DEA")
    with session_scope(ex.factory) as session:
        ex.broker.pause(session, ex.risk.now(), "maintenance drill")
    for _ in range(3):
        ex.dispatcher.dispatch_once()
        ex.risk.clock.advance(timedelta(minutes=1))
    (event,) = outbox(ex, "order.submit")
    assert event.status is OutboxStatus.DEAD
    assert "PAUSED" in (event.last_error or "")
    o = ex.order(oid)
    assert o.status is S.FAILED
    assert ex.event_types(oid)[-1] == "FAILED"
    reservation = ex.rv.get(r)
    assert reservation.status is ReservationStatus.FAILED  # nothing was ever live
    assert books(ex) == 0
    with session_scope(ex.factory) as session:
        health = session.scalar(
            select(SystemHealthEventRecord).where(SystemHealthEventRecord.subject_id == str(oid))
        )
        assert health is not None
        assert health.event_type == "ORDER_SUBMISSION_FAILED"
    assert ExecutionReconciler(ex.factory, ex.risk.clock).run(account).passed


def test_a_crash_mid_handler_rolls_back_and_the_retry_submits_once(ex, monkeypatch):
    account, _r, oid = submitted(ex, "CRA")

    def crash(*args, **kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(ex.service, "_apply_pending", crash)  # after the broker accepted
    with pytest.raises(SimulatedCrash):
        ex.dispatcher.dispatch_once()
    (event,) = outbox(ex, "order.submit")
    assert (event.status, event.attempts) == (OutboxStatus.PENDING, 0)  # never recorded
    assert books(ex) == 0  # the simulator's acceptance rolled back with it
    assert ex.order(oid).status is S.APPROVED
    engine = restarted(ex)  # a new process
    engine.dispatcher().dispatch()
    assert ex.order(oid).status is S.OPEN
    assert books(ex) == 1
    assert ExecutionReconciler(ex.factory, ex.risk.clock).run(account).passed


def test_order_state_survives_a_restart_and_lost_deliveries_are_recovered(ex):
    account, r, oid = submitted(ex, "RSA")
    ex.dispatch()
    ex.fill(oid, "5", "99.50", "rsa-fill-0001", apply=False)  # recorded at the broker only
    ex.fill(oid, "7", "99.60", "rsa-fill-0002", apply=False)
    assert ex.order(oid).filled_quantity == 0
    # The deliveries of both broker events are lost (marked delivered without applying).
    with session_scope(ex.factory) as session:
        session.execute(
            text(
                "UPDATE outbox_events SET status = 'DELIVERED', delivered_at = now(), "
                "attempts = attempts + 1 WHERE topic = :t AND status = 'PENDING'"
            ),
            {"t": BROKER_TOPIC},
        )
    with session_scope(ex.factory) as session:
        ex.broker.shutdown(session, ex.risk.now())
    engine = restarted(ex)
    report = engine.recover()
    assert report.broker_events_applied == 2
    assert report.errors == []
    o = ex.order(oid)
    assert (o.status, o.filled_quantity) == (S.PARTIALLY_FILLED, 12)
    assert ex.rv.get(r).consumed_quantity == 12
    with session_scope(ex.factory) as session:
        assert engine.broker.health(session).status is BrokerRunState.RUNNING
    assert engine.recover().broker_events_applied == 0  # idempotent
    engine.dispatcher().dispatch()
    assert ExecutionReconciler(ex.factory, ex.risk.clock).run(account).passed


def test_broker_events_are_applied_in_broker_order_whatever_the_delivery_order(ex):
    _account, _r, oid = submitted(ex, "ORD")
    ex.dispatch()
    ex.fill(oid, "5", "99.50", "ord-fill-0001", apply=False)
    ex.fill(oid, "15", "99.60", "ord-fill-0002", apply=False)  # completes the order
    with session_scope(ex.factory) as session:
        last = session.scalar(
            select(PaperBrokerEventRecord)
            .where(PaperBrokerEventRecord.client_order_id == oid)
            .order_by(PaperBrokerEventRecord.sequence.desc())
            .limit(1)
        )
        assert last is not None
        message = OutboxMessage(
            0,
            BROKER_TOPIC,
            "order",
            str(oid),
            {"broker_event_id": str(last.id), "client_order_id": str(oid)},
            0,
        )
        ex.service.handle_broker_event(session, message)  # the later event arrives first
    events = ex.events(oid)
    assert [e.event_type.value for e in events[-2:]] == ["FILL", "FILL"]
    assert [e.quantity for e in events[-2:]] == [5, 15]
    assert ex.order(oid).status is S.FILLED
    summary = ex.dispatch()  # both original deliveries: no-ops
    assert summary.failed == summary.dead == 0
    assert len([e for e in ex.events(oid) if e.event_type.value == "FILL"]) == 2


def test_a_rolled_back_transaction_leaves_nothing(ex):
    account = ex.account()
    r = ex.reservation(account, ex.asset("RBK"), "rbk-res-0001")

    class Abort(Exception):
        pass

    def submit_then_abort() -> None:
        with session_scope(ex.factory) as session:
            result = ex.service.submit_in(
                session,
                ex.entry(account, r, "20"),
                idempotency_key="rbk-0001",
                requested_by="operator:t",
            )
            assert result.status is S.APPROVED
            raise Abort

    with pytest.raises(Abort):
        submit_then_abort()
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord) == 0
        assert ex.count(session, OutboxEventRecord) == 0
    reservation = ex.rv.get(r)
    assert reservation.execution_link is ExecutionLink.NONE
    again = ex.submit(ex.entry(account, r, "20"), "rbk-0001")  # the same key works afresh
    assert again.created
    assert again.status is S.APPROVED


def test_duplicate_events_are_suppressed(ex):
    _account, _r, oid = submitted(ex, "DPE")
    with session_scope(ex.factory) as session:
        again = enqueue(
            session,
            topic="order.submit",
            aggregate_type="order",
            aggregate_id=oid,
            dedupe_key=f"order.submit:{oid}",
            payload={"order_id": str(oid)},
            at=ex.risk.now(),
        )
        assert again is False
        # A redundant delivery under another key is consumed idempotently.
        assert enqueue(
            session,
            topic="order.submit",
            aggregate_type="order",
            aggregate_id=oid,
            dedupe_key=f"order.submit:{oid}:replayed",
            payload={"order_id": str(oid)},
            at=ex.risk.now(),
        )
    summary = ex.dispatch()
    assert summary.failed == summary.dead == 0
    assert books(ex) == 1
    assert ex.event_types(oid).count("ACCEPTED") == 1


def test_an_unconfirmed_cancellation_holds_the_reservation_and_trips_execution_safety(ex):
    account, r, oid = submitted(ex, "UCC")
    ex.dispatch()
    ex.cancel(oid, "ucc-cancel-0001")
    with session_scope(ex.factory) as session:
        ex.broker.pause(session, ex.risk.now(), "broker unreachable drill")
    for _ in range(3):
        ex.dispatcher.dispatch_once()
        ex.risk.clock.advance(timedelta(minutes=1))
    assert ex.order(oid).status is S.CANCEL_PENDING  # never assumed cancelled
    held = ex.rv.get(r)
    assert (held.status, held.execution_link) == (
        ReservationStatus.APPROVED,
        ExecutionLink.UNCERTAIN,
    )
    assert held.remaining_quantity == held.approved_quantity  # nothing released
    with session_scope(ex.factory) as session:
        assert breaker_statuses(session, account)[BreakerType.EXECUTION_SAFETY] is (
            BreakerStatus.TRIPPED
        )
    report = restarted(ex).recover()  # the broker is back
    assert report.cancellations_retried == [oid]
    assert ex.order(oid).status is S.CANCELLED
    assert ex.rv.get(r).status is ReservationStatus.RELEASED


def test_submissions_wait_while_the_broker_is_stopped(ex):
    _account, _r, oid = submitted(ex, "STP")
    with session_scope(ex.factory) as session:
        ex.broker.shutdown(session, ex.risk.now())
        health = ex.broker.health(session)
        assert (health.status, health.accepting_orders, health.simulated) == (
            BrokerRunState.STOPPED,
            False,
            True,
        )
    result = ex.dispatcher.dispatch_once()
    assert result is not None
    assert result.status is OutboxStatus.PENDING
    assert ex.order(oid).status is S.APPROVED
    with session_scope(ex.factory) as session:
        ex.broker.start(session, ex.risk.now())
    ex.risk.clock.advance(timedelta(seconds=5))
    ex.dispatch()
    assert ex.order(oid).status is S.OPEN

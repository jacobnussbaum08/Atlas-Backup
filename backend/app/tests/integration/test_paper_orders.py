"""Phase 7A paper order management against a real PostgreSQL server.

TEST FIXTURE data. Every order is a simulated paper order handled by the isolated paper
broker; every fill is a controlled test execution event (Phase 7A has no fill model).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.accounting.posting import load_state
from app.database.models.execution import (
    ExecutionRequestRecord,
    OrderEventRecord,
    OrderRecord,
    OutboxEventRecord,
    PaperBrokerOrderRecord,
)
from app.database.models.reservations import RiskReservationEventRecord
from app.database.sessions import session_scope
from app.execution import state_machine
from app.execution.enums import (
    BrokerOrderStatus,
    CancelMode,
    ExecutionDecision,
    OrderEventSource,
    OrderEventType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.execution.reconciliation import ExecutionReconciler
from app.execution.service import FillRefusedError, IdempotencyConflictError
from app.risk.enums import (
    ExecutionLink,
    ReleaseResolution,
    ReservationEventType,
    ReservationStatus,
    TradeDirection,
)
from app.tests.integration.execution_support import COORDINATOR, Execution, make_execution

pytestmark = pytest.mark.integration
S = OrderStatus
REFUSED, CREATED = ExecutionDecision.REFUSED, ExecutionDecision.ORDER_CREATED


@pytest.fixture
def ex(fresh_engine) -> Execution:
    return make_execution(fresh_engine)


@pytest.fixture
def manual(fresh_engine) -> Execution:
    return make_execution(fresh_engine, cancel_mode=CancelMode.MANUAL)


def codes(result) -> set[str]:
    return {r["code"] for r in result.reasons}


def position(ex: Execution, account: uuid.UUID, asset: uuid.UUID) -> D:
    with session_scope(ex.factory) as session:
        p = load_state(session, account, "USD").position(asset)
        return D(0) if p is None else p.quantity


def reservation_events(ex: Execution, reservation: uuid.UUID) -> list[str]:
    return [e.event_type.value for e in ex.rv.events(reservation)]


def assert_reconciled(ex: Execution, account: uuid.UUID) -> None:
    result = ExecutionReconciler(ex.factory, ex.risk.clock).run(account)
    assert result.findings == [], result.findings
    assert ex.risk.reconcile(account) == "PASSED"  # 6A double-entry reconciliation
    recon = ex.rv.reconciler.run(account, actor="system:test")
    assert recon.status.value == "PASSED", recon  # 6C reservation reconciliation


# ------------------------------------------------------------------ lifecycle
def test_entry_order_lifecycle_consumes_the_reservation_by_actual_fills(ex):
    account, asset = ex.account(), ex.asset("PXA")
    r = ex.reservation(account, asset, "life-res-0001")
    res = ex.submit(ex.entry(account, r, "92"), "life-order-0001")
    assert (res.decision, res.status, res.created) == (CREATED, S.APPROVED, True)
    order_id = res.order_id
    reservation = ex.rv.get(r)
    assert reservation.execution_link is ExecutionLink.ORDER_ATTACHED
    assert reservation.order_ref == str(order_id)
    assert ex.pending_outbox() == 1  # order.submit, written in the same transaction

    ex.dispatch()
    o = ex.order(order_id)
    assert o.status is S.OPEN
    assert o.broker_order_id is not None
    assert o.protection_price == D("99.70")  # the approval's worst entry price

    ex.fill(order_id, "40", "99.50", "life-fill-0001")
    o = ex.order(order_id)
    assert (o.status, o.filled_quantity, o.remaining_quantity) == (S.PARTIALLY_FILLED, 40, 52)
    reservation = ex.rv.get(r)
    assert reservation.status is ReservationStatus.PARTIALLY_CONSUMED
    assert (reservation.consumed_quantity, reservation.remaining_quantity) == (40, 52)
    assert position(ex, account, asset) == 40  # exposure moved, not duplicated

    ex.fill(order_id, "52", "99.60", "life-fill-0002")
    o = ex.order(order_id)
    assert (o.status, o.filled_quantity, o.remaining_quantity) == (S.FILLED, 92, 0)
    assert o.average_fill_price == D("99.5565217391")  # (40 x 99.50 + 52 x 99.60) / 92
    assert o.closed_at is not None
    reservation = ex.rv.get(r)
    assert reservation.status is ReservationStatus.CONSUMED
    assert reservation.remaining_quantity == 0
    assert position(ex, account, asset) == 92
    assert ex.event_types(order_id) == ["CREATED", "APPROVED", "ACCEPTED", "FILL", "FILL"]
    assert [e.source for e in ex.events(order_id)] == [
        OrderEventSource.OPERATOR,
        OrderEventSource.EXECUTION_ENGINE,
        OrderEventSource.PAPER_BROKER,
        OrderEventSource.PAPER_BROKER,
        OrderEventSource.PAPER_BROKER,
    ]
    fills = ex.fills(order_id)
    assert [f.quantity for f in fills] == [40, 52]
    assert all(f.reservation_event_id is not None for f in fills)
    summary = ex.dispatch()  # the broker events' own deliveries: already applied, no-ops
    assert summary.delivered >= 2
    assert ex.event_types(order_id)[-1] == "FILL"
    assert_reconciled(ex, account)


def test_every_state_change_is_recorded_with_timestamp_source_and_reason(ex):
    account, asset = ex.account(), ex.asset("PXB")
    r = ex.reservation(account, asset, "hist-res-0001")
    oid = ex.open_order(account, r, "10", "hist-order-0001")
    for e in ex.events(oid):
        assert e.occurred_at is not None
        assert e.source is not None
        assert len(e.reason) >= 3
        assert e.actor
    seq = [e.sequence for e in ex.events(oid)]
    assert seq == list(range(1, len(seq) + 1))


@pytest.mark.parametrize(
    ("order_type", "fields"),
    [
        (OrderType.MARKET, {}),
        (OrderType.LIMIT, {"limit_price": "99.50"}),
        (OrderType.STOP, {"stop_price": "99.70"}),
        (OrderType.STOP_LIMIT, {"stop_price": "99.60", "limit_price": "99.70"}),
        (OrderType.TRAILING_STOP, {"trail_amount": "0.50"}),
        (OrderType.TRAILING_STOP, {"trail_percent": "1.5"}),
    ],
)
def test_entry_order_types_are_accepted_with_their_fields(ex, order_type, fields):
    account, asset = ex.account(), ex.asset("TYP")
    r = ex.reservation(account, asset, "type-res-0001", requested_quantity=D(10))
    res = ex.submit(ex.entry(account, r, "10", order_type, **fields), "type-order-0001")
    assert res.decision is CREATED, res.reasons
    ex.dispatch()
    o = ex.order(res.order_id)
    assert o.order_type is order_type
    assert o.status is S.OPEN
    assert o.limit_price == (D(fields["limit_price"]) if "limit_price" in fields else None)
    assert o.stop_price == (D(fields["stop_price"]) if "stop_price" in fields else None)
    with session_scope(ex.factory) as session:
        book = session.scalar(
            select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == o.id)
        )
        assert book is not None
        assert book.order_type is order_type
        assert book.simulated is True


def test_a_limit_fill_beyond_the_limit_is_refused(ex):
    account, asset = ex.account(), ex.asset("LIM")
    r = ex.reservation(account, asset, "lim-res-0001")
    oid = ex.open_order(
        account, r, "20", "lim-order-0001", order_type=OrderType.LIMIT, limit_price="99.00"
    )
    with pytest.raises(FillRefusedError, match="beyond the limit"):
        ex.fill(oid, "5", "99.01", "lim-fill-0001")
    mkt = ex.reservation(account, ex.asset("LIM2"), "lim-res-0002")
    market = ex.open_order(account, mkt, "5", "lim-order-0002")
    with pytest.raises(FillRefusedError, match="worst entry"):
        ex.fill(market, "5", "99.71", "lim-fill-0002")
    assert ex.order(oid).filled_quantity == 0
    assert position(ex, account, asset) == 0


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    ("quantity", "order_type", "fields", "code"),
    [
        ("93", OrderType.MARKET, {}, "QUANTITY_ABOVE_APPROVAL"),
        ("1.5", OrderType.MARKET, {}, "QUANTITY_PRECISION"),
        ("10", OrderType.LIMIT, {"limit_price": "99.705"}, "PRICE_PRECISION"),
        ("10", OrderType.LIMIT, {"limit_price": "99.80"}, "PRICE_BEYOND_PROTECTION"),
        ("10", OrderType.STOP, {"stop_price": "99.71"}, "PRICE_BEYOND_PROTECTION"),
        (
            "10",
            OrderType.STOP_LIMIT,
            {"stop_price": "99.60", "limit_price": "99.50"},
            "STOP_LIMIT_INCONSISTENT",
        ),
        ("10", OrderType.LIMIT, {}, "PRICE_FIELDS"),
        ("10", OrderType.TAKE_PROFIT, {"limit_price": "105.00"}, "TAKE_PROFIT_IS_EXIT"),
        ("10", OrderType.MARKET, {"side": OrderSide.SELL_SHORT}, "SIDE_MISMATCH"),
        ("10", OrderType.MARKET, {"asset_id": uuid.uuid4()}, "ASSET_MISMATCH"),
        ("10", OrderType.MARKET, {"strategy_proposal_id": uuid.uuid4()}, "ORIGIN_MISMATCH"),
    ],
)
def test_invalid_orders_are_refused_and_the_reservation_is_untouched(
    ex, quantity, order_type, fields, code
):
    account, asset = ex.account(), ex.asset("BAD")
    r = ex.reservation(account, asset, "bad-res-0001")
    res = ex.submit(ex.entry(account, r, quantity, order_type, **fields), "bad-order-0001")
    assert res.decision is REFUSED
    assert res.order_id is None
    assert code in codes(res), res.reasons
    reservation = ex.rv.get(r)
    assert (reservation.status, reservation.execution_link) == (
        ReservationStatus.APPROVED,
        ExecutionLink.NONE,
    )
    with session_scope(ex.factory) as session:
        request = session.get(ExecutionRequestRecord, res.request_id)
        assert request is not None
        assert request.decision is REFUSED
        assert ex.count(session, OrderRecord) == 0
        assert ex.count(session, OutboxEventRecord) == 0


def test_expiry_bounds_are_enforced(ex):
    account, asset = ex.account(), ex.asset("EXB")
    r = ex.reservation(account, asset, "exb-res-0001")
    now = ex.risk.now()
    past = ex.submit(ex.entry(account, r, "10", expires_at=now - timedelta(minutes=1)), "exb-0001")
    far = ex.submit(ex.entry(account, r, "10", expires_at=now + timedelta(days=8)), "exb-0002")
    assert "EXPIRY_INVALID" in codes(past)
    assert "EXPIRY_INVALID" in codes(far)
    ok = ex.submit(ex.entry(account, r, "10", expires_at=now + timedelta(days=7)), "exb-0003")
    assert ok.decision is CREATED


# ------------------------------------------------------------------ authorization
@pytest.mark.parametrize(
    "submitter",
    ["strategy:momentum", "system:strategy-worker", "system:ai-decision-engine", "llm:claude"],
)
def test_strategies_and_llms_cannot_submit(ex, submitter):
    account, asset = ex.account(), ex.asset("UNA")
    r = ex.reservation(account, asset, "una-res-0001")
    res = ex.submit(ex.entry(account, r, "10"), "una-order-0001", by=submitter)
    assert res.decision is REFUSED
    assert codes(res) == {"UNAUTHORIZED_SUBMITTER"}
    assert ex.rv.get(r).execution_link is ExecutionLink.NONE


def test_the_coordinator_executes_strategy_reservations_only(ex):
    account = ex.account()
    manual_r = ex.reservation(account, ex.asset("COA"), "coa-res-0001")
    refused = ex.submit(ex.entry(account, manual_r, "10"), "coa-order-0001", by=COORDINATOR)
    assert codes(refused) == {"OPERATOR_INTENT"}  # an operator's intent needs an operator
    r, proposal = ex.strategy_reservation(account, ex.asset("COB"), "coa-res-0002")
    res = ex.submit(
        ex.entry(account, r, "10", strategy_proposal_id=proposal), "coa-order-0002", by=COORDINATOR
    )
    assert res.decision is CREATED, res.reasons
    o = ex.order(res.order_id)
    assert o.strategy_proposal_id == proposal
    assert o.requested_by == COORDINATOR


def test_reservations_are_verified(ex):
    account = ex.account()
    other = ex.risk.account("someone-else")
    # missing, other account, rejected, released, expired
    missing = ex.submit(ex.entry(account, uuid.uuid4(), "10"), "ver-0001")
    assert codes(missing) == {"RESERVATION_NOT_FOUND"}
    foreign = ex.reservation(other, ex.asset("VEA"), "ver-res-0001")
    assert codes(ex.submit(ex.entry(account, foreign, "10"), "ver-0002")) == {
        "RESERVATION_OTHER_ACCOUNT"
    }
    rejected = ex.rv.reserve(account, ex.risk.proposal(ex.asset("VEB"), "99.70", None), "ver-res-2")
    assert rejected.decision is ReservationStatus.REJECTED
    res = ex.submit(ex.entry(account, rejected.reservation_id, "1"), "ver-0003")
    assert "RESERVATION_NOT_ACTIVE" in codes(res)
    released = ex.reservation(account, ex.asset("VEC"), "ver-res-0003")
    ex.rv.service.release(
        released,
        resolution=ReleaseResolution.NOT_SUBMITTED,
        idempotency_key="ver-release-1",
        actor="system:test-worker",
        reason="not submitted (controlled test)",
    )
    assert "RESERVATION_NOT_ACTIVE" in codes(
        ex.submit(ex.entry(account, released, "1"), "ver-0004")
    )
    expiring = ex.reservation(account, ex.asset("VED"), "ver-res-0004")
    ex.risk.clock.advance(timedelta(seconds=301))
    assert "RESERVATION_EXPIRED" in codes(ex.submit(ex.entry(account, expiring, "1"), "ver-0005"))


def test_a_consumed_reservation_cannot_be_executed_again(ex):
    account, asset = ex.account(), ex.asset("CON")
    r = ex.reservation(account, asset, "con-res-0001")
    oid = ex.open_order(account, r, "92", "con-order-0001")
    ex.fill(oid, "92", "99.70", "con-fill-0001")
    assert ex.rv.get(r).status is ReservationStatus.CONSUMED
    again = ex.submit(ex.entry(account, r, "10"), "con-order-0002")
    assert {"DUPLICATE_ORDER", "RESERVATION_NOT_ACTIVE"} <= codes(again)


# ------------------------------------------------------------------ idempotency
def test_duplicate_submissions_create_one_order(ex):
    account, asset = ex.account(), ex.asset("DUP")
    r = ex.reservation(account, asset, "dup-res-0001")
    request = ex.entry(account, r, "50")
    first = ex.submit(request, "dup-order-0001")
    again = ex.submit(request, "dup-order-0001")
    assert (again.order_id, again.created, again.decision) == (first.order_id, False, CREATED)
    with pytest.raises(IdempotencyConflictError):
        ex.submit(ex.entry(account, r, "51"), "dup-order-0001")
    other_key = ex.submit(request, "dup-order-0002")
    assert other_key.decision is REFUSED
    assert "DUPLICATE_ORDER" in codes(other_key)
    assert reservation_events(ex, r).count("ORDER_ATTACHED") == 1
    ex.dispatch()
    ex.dispatch()
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord) == 1
        assert ex.count(session, PaperBrokerOrderRecord) == 1
    # A duplicate controlled test event is recorded once and applied once.
    ex.fill(first.order_id, "10", "99.50", "dup-fill-0001")
    ex.fill(first.order_id, "10", "99.50", "dup-fill-0001")
    assert ex.order(first.order_id).filled_quantity == 10
    with pytest.raises(Exception, match="other values"):
        ex.fill(first.order_id, "11", "99.50", "dup-fill-0001")


# ------------------------------------------------------------------ revalidation
def test_revalidation_rejects_entries_while_a_breaker_is_tripped_but_allows_exits(ex):
    account = ex.account()
    a, b = ex.asset("RVA"), ex.asset("RVB")
    filled_r = ex.reservation(account, a, "rv-res-0001")
    parent = ex.open_order(account, filled_r, "50", "rv-order-0001")
    ex.fill(parent, "50", "99.70", "rv-fill-0001")
    ex.dispatch()  # includes the 6A reconciliation after the fill (POST_FILL)
    pending = ex.reservation(account, b, "rv-res-0002")
    assert ex.risk.service.engage_kill_switch(account, actor="operator:test", reason="drill")
    res = ex.submit(ex.entry(account, pending, "10"), "rv-order-0002")
    assert (res.decision, res.status) == (CREATED, S.REJECTED)
    assert "RISK_HALTED" in codes(res)
    rv = ex.rv.get(pending)
    assert rv.status is ReservationStatus.RELEASED  # never attached: NOT_SUBMITTED
    assert ex.rv.events(pending)[-1].resolution is ReleaseResolution.NOT_SUBMITTED
    # Halting new entries does not prevent reducing a position.
    exit_res = ex.submit(ex.exit(account, parent, "50"), "rv-exit-0001")
    assert exit_res.status is S.APPROVED, exit_res.reasons
    ex.dispatch()
    ex.fill(exit_res.order_id, "50", "101.00", "rv-exit-fill-1")
    assert position(ex, account, a) == 0
    # And no entry fill happens while halted.
    other = ex.order(parent)
    assert other.status is S.FILLED


# ------------------------------------------------------------------ cancellation
def test_cancellation_releases_only_after_the_broker_confirms(manual):
    ex = manual
    account, asset = ex.account(), ex.asset("CNL")
    r = ex.reservation(account, asset, "cnl-res-0001")
    oid = ex.open_order(account, r, "92", "cnl-order-0001")
    ex.fill(oid, "30", "99.50", "cnl-fill-0001")
    result = ex.cancel(oid, "cnl-cancel-0001")
    assert result.status is S.CANCEL_PENDING
    ex.dispatch()  # the broker acknowledges only (MANUAL): nothing is released
    assert ex.order(oid).status is S.CANCEL_PENDING
    held = ex.rv.get(r)
    assert (held.status, held.remaining_quantity) == (ReservationStatus.PARTIALLY_CONSUMED, 62)
    ex.fill(oid, "10", "99.60", "cnl-fill-0002")  # still fills while the cancel is pending
    assert ex.order(oid).status is S.CANCEL_PENDING
    assert ex.rv.get(r).consumed_quantity == 40
    ex.service.confirm_controlled_cancel(oid, external_ref="cnl-confirm-001", actor="system:t")
    o = ex.order(oid)
    assert (o.status, o.filled_quantity, o.remaining_quantity) == (S.CANCELLED, 40, 52)
    done = ex.rv.get(r)
    assert (done.status, done.consumed_quantity, done.released_quantity) == (
        ReservationStatus.RELEASED,
        40,
        52,
    )
    assert ex.rv.events(r)[-1].resolution is ReleaseResolution.ORDER_CANCELLED
    assert position(ex, account, asset) == 40  # consumed exposure stays in the ledger
    assert ex.event_types(oid) == [
        "CREATED",
        "APPROVED",
        "ACCEPTED",
        "FILL",
        "CANCEL_REQUESTED",
        "FILL",
        "CANCEL_CONFIRMED",
    ]
    assert_reconciled(ex, account)


def test_immediate_cancellation_and_cancellation_before_receipt(ex):
    account = ex.account()
    r1 = ex.reservation(account, ex.asset("CIA"), "ci-res-0001")
    oid = ex.open_order(account, r1, "20", "ci-order-0001")
    ex.cancel(oid, "ci-cancel-0001")
    assert ex.rv.get(r1).status is ReservationStatus.APPROVED  # not before confirmation
    ex.dispatch()
    assert ex.order(oid).status is S.CANCELLED
    assert ex.rv.get(r1).status is ReservationStatus.RELEASED
    # Cancelled while the submission is still queued: the broker records a tombstone and
    # never works the order.
    r2 = ex.reservation(account, ex.asset("CIB"), "ci-res-0002")
    queued = ex.submit(ex.entry(account, r2, "20"), "ci-order-0002").order_id
    ex.cancel(queued, "ci-cancel-0002")
    ex.dispatch()
    assert ex.order(queued).status is S.CANCELLED
    with session_scope(ex.factory) as session:
        book = session.scalar(
            select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == queued)
        )
        assert book is not None
        assert book.status is BrokerOrderStatus.CANCELLED
        assert book.filled_quantity == 0
    assert ex.event_types(queued) == ["CREATED", "APPROVED", "CANCEL_REQUESTED", "CANCEL_CONFIRMED"]
    # Idempotent; and nothing final can be cancelled.
    assert ex.cancel(oid, "ci-cancel-0001").created is False
    with pytest.raises(state_machine.InvalidOrderTransitionError):
        ex.cancel(oid, "ci-cancel-0003")
    with pytest.raises(Exception, match="may not cancel"):
        ex.cancel(queued, "ci-cancel-0004", by="strategy:momentum")


def test_a_full_fill_overtakes_a_pending_cancellation(manual):
    ex = manual
    account, asset = ex.account(), ex.asset("OVT")
    r = ex.reservation(account, asset, "ovt-res-0001")
    oid = ex.open_order(account, r, "30", "ovt-order-0001")
    ex.cancel(oid, "ovt-cancel-0001")
    ex.dispatch()
    ex.fill(oid, "30", "99.70", "ovt-fill-0001")
    o = ex.order(oid)
    assert o.status is S.FILLED
    r_after = ex.rv.get(r)
    # 30 of 92 consumed, the 62 never ordered released as ORDER_COMPLETED
    assert (r_after.consumed_quantity, r_after.released_quantity) == (30, 62)
    ex.dispatch()  # a late cancel delivery finds nothing to do
    assert ex.order(oid).status is S.FILLED


# ------------------------------------------------------------------ expiration
def test_orders_expire_and_release_only_unfilled_exposure(ex):
    account = ex.account()
    now = ex.risk.now()
    r1 = ex.reservation(account, ex.asset("EXA"), "exp-res-0001")
    r2 = ex.reservation(account, ex.asset("EXC"), "exp-res-0002")
    working = ex.open_order(
        account, r2, "40", "exp-order-0002", expires_at=now + timedelta(minutes=10)
    )
    ex.fill(working, "15", "99.40", "exp-fill-0001")
    queued = ex.submit(  # never delivered to the paper broker
        ex.entry(account, r1, "20", expires_at=now + timedelta(minutes=10)), "exp-order-0001"
    ).order_id
    ex.risk.clock.advance(timedelta(minutes=5, seconds=1))
    # Reservations with orders attached never expire by themselves.
    summary = ex.rv.service.expire_due(account)
    assert summary.expired == []
    assert set(summary.deferred) == {r1, r2}
    assert ex.service.expire_due().expired == []  # orders not due yet
    ex.risk.clock.advance(timedelta(minutes=5))
    report = ex.service.expire_due()
    assert set(report.expired) == {queued, working}
    assert ex.order(queued).status is S.EXPIRED
    assert ex.event_types(queued)[-1] == "EXPIRED"
    assert ex.events(queued)[-1].source is OrderEventSource.EXPIRY  # never reached the broker
    w = ex.order(working)
    assert (w.status, w.filled_quantity) == (S.EXPIRED, 15)
    assert ex.events(working)[-1].source is OrderEventSource.PAPER_BROKER
    for r, consumed in ((r1, 0), (r2, 15)):
        rr = ex.rv.get(r)
        assert rr.status is ReservationStatus.RELEASED
        assert rr.consumed_quantity == consumed
        assert ex.rv.events(r)[-1].resolution is ReleaseResolution.ORDER_EXPIRED
    ex.dispatch()  # the queued submission is now a no-op
    with session_scope(ex.factory) as session:
        assert (
            session.scalar(
                select(PaperBrokerOrderRecord).where(
                    PaperBrokerOrderRecord.client_order_id == queued
                )
            )
            is None
        )
    assert_reconciled(ex, account)


def test_a_partly_used_reservation_releases_the_rest_when_the_order_completes(ex):
    account, asset = ex.account(), ex.asset("PRT")
    r = ex.reservation(account, asset, "prt-res-0001")
    oid = ex.open_order(account, r, "50", "prt-order-0001")
    ex.fill(oid, "50", "99.70", "prt-fill-0001")
    rr = ex.rv.get(r)
    assert (rr.status, rr.consumed_quantity, rr.released_quantity) == (
        ReservationStatus.RELEASED,
        50,
        42,
    )
    assert ex.rv.events(r)[-1].resolution is ReleaseResolution.ORDER_COMPLETED
    state = ex.rv.state(account)
    assert state.active_count == 0
    assert state.reserved_risk == 0  # nothing double-counted: the 50 are a ledger position


# ------------------------------------------------------------------ exits and portfolio
def test_exit_orders_reduce_the_position_and_realize_pnl(ex):
    account, asset = ex.account(), ex.asset("EXT")
    r = ex.reservation(account, asset, "ext-res-0001")
    parent = ex.open_order(account, r, "92", "ext-order-0001")
    ex.fill(parent, "92", "99.50", "ext-fill-0001", commission="1.00", fees="0.10")
    with session_scope(ex.factory) as session:
        state = load_state(session, account, "USD")
        pos = state.position(asset)
        assert pos is not None
        assert (pos.quantity, pos.cost) == (D(92), D("9154.00"))  # 92 x 99.50, costs expensed
        assert state.normal_balance("1000", "USD")[0] == D("100000.00") - D("9154.00") - D("1.10")
        assert state.normal_balance("5000", "USD", asset)[0] == D("1.00")
        assert state.normal_balance("5100", "USD", asset)[0] == D("0.10")
    tp = ex.submit(
        ex.exit(account, parent, "50", OrderType.TAKE_PROFIT, limit_price="105.00"), "ext-tp-0001"
    )
    assert tp.status is S.APPROVED, tp.reasons
    too_much = ex.submit(ex.exit(account, parent, "43"), "ext-tp-0002")  # 92 - 50 live = 42
    assert "QUANTITY_ABOVE_POSITION" in codes(too_much)
    ex.dispatch()
    with pytest.raises(FillRefusedError, match="beyond the limit"):
        ex.fill(tp.order_id, "50", "104.99", "ext-tp-fill-01")
    ex.fill(tp.order_id, "50", "105.00", "ext-tp-fill-02")
    assert ex.order(tp.order_id).status is S.FILLED
    assert ex.fills(tp.order_id)[0].reservation_event_id is None  # an exit consumes nothing
    with session_scope(ex.factory) as session:
        state = load_state(session, account, "USD")
        assert state.position(asset).quantity == 42  # type: ignore[union-attr]
        assert state.normal_balance("4000", "USD", asset)[0] == D("275.00")  # 50 x 5.50
    rest = ex.submit(
        ex.exit(account, parent, "42", OrderType.TRAILING_STOP, trail_percent="2"), "ext-ts-0001"
    )
    ex.dispatch()
    ex.fill(rest.order_id, "42", "98.00", "ext-ts-fill-01")
    assert position(ex, account, asset) == 0
    assert ex.order(parent).status is S.FILLED
    assert ex.rv.get(r).status is ReservationStatus.CONSUMED  # untouched by exits
    nothing = ex.submit(ex.exit(account, parent, "1"), "ext-none-0001")
    assert "QUANTITY_ABOVE_POSITION" in codes(nothing)
    assert_reconciled(ex, account)


def test_exits_are_validated(ex):
    account = ex.account()
    r = ex.reservation(account, ex.asset("EXV"), "exv-res-0001")
    unfilled = ex.open_order(account, r, "10", "exv-order-0001")
    assert codes(ex.submit(ex.exit(account, unfilled, "1"), "exv-0001")) == {"NOTHING_TO_CLOSE"}
    assert codes(ex.submit(ex.exit(account, uuid.uuid4(), "1"), "exv-0002")) == {"PARENT_NOT_FOUND"}
    no_parent = ex.submit(
        ex.exit(account, unfilled, "1").model_copy(update={"parent_order_id": None}), "exv-0003"
    )
    assert codes(no_parent) == {"PARENT_REQUIRED"}
    ex.fill(unfilled, "10", "99.70", "exv-fill-0001")
    wrong_side = ex.submit(ex.exit(account, unfilled, "1", side=OrderSide.BUY), "exv-0004")
    assert "SIDE_MISMATCH" in codes(wrong_side)


def test_short_entries_post_short_positions(fresh_engine):
    ex = make_execution(fresh_engine)
    risk = ex.risk
    account = risk.account(configure=False)
    risk.configure(account, missing_margin_policy="USE_CONFIGURED_MINIMUM")
    risk.reconcile(account)
    asset = risk.asset("SHT", start=50.3)
    r = ex.reservation(
        account, asset, "sht-res-0001", entry="50.00", stop="52.00", direction=TradeDirection.SHORT
    )
    rr = ex.rv.get(r)
    oid = ex.open_order(account, r, str(rr.approved_quantity.normalize()), "sht-order-0001")
    o = ex.order(oid)
    assert (o.side, o.direction) == (OrderSide.SELL_SHORT, TradeDirection.SHORT)
    ex.fill(oid, "10", "50.00", "sht-fill-0001")
    assert position(ex, account, asset) == -10
    with pytest.raises(FillRefusedError, match="worst entry"):
        ex.fill(oid, "1", "49.99", "sht-fill-0002")  # a short fill below the protection
    cover = ex.submit(ex.exit(account, oid, "10"), "sht-cover-0001")
    assert cover.order_id is not None
    ex.dispatch()
    ex.fill(cover.order_id, "10", "49.00", "sht-cover-fill-1")
    assert position(ex, account, asset) == 0
    assert ex.order(cover.order_id).side is OrderSide.BUY_TO_COVER


# ------------------------------------------------------------------ database guards
def _approved_order(ex: Execution, symbol: str) -> uuid.UUID:
    account = ex.account()
    r = ex.reservation(account, ex.asset(symbol), f"{symbol.lower()}-res-0001")
    result = ex.submit(ex.entry(account, r, "10"), f"{symbol.lower()}-order-0001")
    assert result.order_id is not None
    return result.order_id


def _raw_event(ex: Execution, order_id: uuid.UUID, event: OrderEventType, target: OrderStatus):
    o = ex.order(order_id)
    with session_scope(ex.factory) as session:
        session.add(
            OrderEventRecord(
                order_id=order_id,
                sequence=o.last_event_sequence + 1,
                event_type=event,
                from_status=o.status,
                to_status=target,
                source=OrderEventSource.OPERATOR,
                actor="operator:tamper",
                reason="tampering attempt",
                occurred_at=ex.risk.now(),
            )
        )


def test_the_database_refuses_direct_changes_to_order_history(ex):
    oid = _approved_order(ex, "GDA")
    for statement in (
        "UPDATE orders SET status = 'FILLED' WHERE id = :id",
        "DELETE FROM orders WHERE id = :id",
        "UPDATE order_events SET reason = 'rewritten' WHERE order_id = :id",
        "DELETE FROM order_events WHERE order_id = :id",
        "UPDATE execution_requests SET decision = 'REFUSED'",
    ):
        with pytest.raises(DBAPIError), session_scope(ex.factory) as session:
            session.execute(text(statement), {"id": oid})
    with pytest.raises(DBAPIError, match="illegal order transition"):
        _raw_event(ex, oid, OrderEventType.CANCEL_CONFIRMED, S.CANCELLED)
    with pytest.raises(DBAPIError, match="must come from a paper broker event"):
        _raw_event(ex, oid, OrderEventType.ACCEPTED, S.OPEN)
    ex.dispatch()
    with pytest.raises(DBAPIError, match="being worked by the paper broker"):
        _raw_event(ex, oid, OrderEventType.EXPIRED, S.EXPIRED)
    with (
        pytest.raises(DBAPIError, match="fills_are_tests_or_simulated"),
        session_scope(ex.factory) as session,
    ):
        session.execute(
            text(
                "INSERT INTO paper_broker_events (id, broker_id, broker_order_id, "
                "client_order_id, sequence, event_type, source, quantity, price, reason, "
                "occurred_at) SELECT gen_random_uuid(), broker_id, id, client_order_id, "
                "last_event_sequence, 'FILL', 'SYSTEM', 1, 99, 'a simulated fill model', now() "
                "FROM paper_broker_orders WHERE client_order_id = :id"
            ),
            {"id": oid},
        )
    assert ex.order(oid).status is S.OPEN


def test_a_proposed_or_unattached_order_cannot_commit(ex, monkeypatch):
    from app.execution import service as svc

    original = svc._OrderEvents.append

    def skip_approval(self, order, event_type, *args, **kwargs):
        if event_type is OrderEventType.APPROVED:
            return None
        return original(self, order, event_type, *args, **kwargs)

    account = ex.account()
    r = ex.reservation(account, ex.asset("PRP"), "prp-res-0001")
    monkeypatch.setattr(svc._OrderEvents, "append", skip_approval)
    with pytest.raises(DBAPIError, match="not decided"):
        ex.submit(ex.entry(account, r, "10"), "prp-order-0001")
    monkeypatch.setattr(svc._OrderEvents, "append", original)
    monkeypatch.setattr(ex.rv.service, "attach_order_in", lambda *a, **k: None)
    with pytest.raises(DBAPIError, match="reservation attached"):
        ex.submit(ex.entry(account, r, "10"), "prp-order-0002")
    with session_scope(ex.factory) as session:
        assert ex.count(session, OrderRecord) == 0
        assert ex.count(session, ExecutionRequestRecord) == 0
    assert ex.rv.get(r).execution_link is ExecutionLink.NONE


def test_the_database_transition_function_equals_the_state_machine(ex):
    statuses = [None, *OrderStatus]
    with session_scope(ex.factory) as session:
        allowed = {
            (None if f is None else S(f), OrderEventType(e), S(t))
            for f, e, t in session.execute(
                text(
                    "SELECT f, e, t FROM unnest(CAST(:f AS text[])) f, "
                    "unnest(CAST(:e AS text[])) e, unnest(CAST(:t AS text[])) t "
                    "WHERE atlas_order_transition_allowed(f, e, t)"
                ),
                {
                    "f": [None if s is None else s.value for s in statuses],
                    "e": [e.value for e in OrderEventType],
                    "t": [s.value for s in OrderStatus],
                },
            )
        }
    expected = {
        (f, e, t)
        for f, e, t in itertools.product(statuses, OrderEventType, OrderStatus)
        if state_machine.is_allowed(f, e, t)
    }
    assert allowed == expected


def test_entry_fills_always_consume_their_reservation_in_the_same_transaction(ex):
    account, asset = ex.account(), ex.asset("FCX")
    r = ex.reservation(account, asset, "fcx-res-0001")
    oid = ex.open_order(account, r, "30", "fcx-order-0001")
    ex.fill(oid, "30", "99.60", "fcx-fill-0001")
    fill = ex.fills(oid)[0]
    with session_scope(ex.factory) as session:
        consumed = session.get(RiskReservationEventRecord, fill.reservation_event_id)
        assert consumed is not None
        assert consumed.event_type is ReservationEventType.CONSUMED
        assert consumed.journal_entry_id == fill.journal_entry_id
        assert consumed.order_ref == str(oid)
        assert consumed.quantity == 30
        purpose = session.get(OrderRecord, oid).purpose  # type: ignore[union-attr]
        assert purpose is OrderPurpose.ENTRY


def test_no_entry_fill_is_recorded_while_new_entries_are_halted(ex):
    account, asset = ex.account(), ex.asset("HLT")
    r = ex.reservation(account, asset, "hlt-res-0001")
    oid = ex.open_order(account, r, "30", "hlt-order-0001")
    ex.fill(oid, "10", "99.60", "hlt-fill-0001")
    assert ex.risk.service.engage_kill_switch(account, actor="operator:test", reason="drill")
    with pytest.raises(FillRefusedError, match="circuit breaker"):
        ex.fill(oid, "10", "99.60", "hlt-fill-0002")
    assert ex.order(oid).filled_quantity == 10
    with session_scope(ex.factory) as session:  # nothing reached the paper broker either
        book = session.scalar(
            select(PaperBrokerOrderRecord).where(PaperBrokerOrderRecord.client_order_id == oid)
        )
        assert book is not None
        assert book.filled_quantity == 10
    exit_order = ex.submit(ex.exit(account, oid, "10"), "hlt-exit-0001")
    ex.dispatch()
    ex.fill(exit_order.order_id, "10", "99.00", "hlt-exit-fill-1")  # reducing is allowed
    assert position(ex, account, asset) == 0

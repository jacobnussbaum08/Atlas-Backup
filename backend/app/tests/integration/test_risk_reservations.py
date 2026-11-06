"""Phase 6C reservation lifecycle, financial correctness, database guards and recovery,
against a real PostgreSQL server. TEST FIXTURE data; fills are controlled test events
(6A CONTROLLED_TEST postings); nothing is submitted or executed anywhere."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.accounting.money import ZERO
from app.database.models import AuditLog, JournalEntryRecord
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationEventRecord,
    RiskReservationRecord,
    SystemHealthEventRecord,
)
from app.database.models.risk import RiskAssessmentRecord, RiskEventRecord
from app.database.sessions import session_scope
from app.risk.enums import (
    AssessmentResult,
    ExecutionLink,
    ReconciliationOutcome,
    ReleaseResolution,
    ReservationEventType,
    ReservationStatus,
    TradeDirection,
)
from app.risk.reservations.service import IdempotencyConflictError, ReservationError
from app.risk.state import evaluate
from app.tests.integration.reservation_support import Reservations, make_reservations
from app.tests.integration.risk_support import OPERATOR

pytestmark = pytest.mark.integration
APPROVED, REJECTED = ReservationStatus.APPROVED, ReservationStatus.REJECTED
SHORT = TradeDirection.SHORT


@pytest.fixture
def rv(fresh_engine) -> Reservations:
    return make_reservations(fresh_engine)


def codes(result) -> set[str]:
    return {r["code"] for r in result.reasons}


def ledger_entries(rv: Reservations, account: uuid.UUID) -> int:
    with session_scope(rv.factory) as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(JournalEntryRecord)
                .where(JournalEntryRecord.portfolio_account_id == account)
            )
            or 0
        )


def live_view(rv: Reservations, account: uuid.UUID):
    with session_scope(rv.factory) as session:
        from app.database.models import PortfolioAccount
        from app.risk import governance

        acct = session.get(PortfolioAccount, account)
        assert acct is not None
        config = governance.active_configuration(session, account)
        return evaluate(session, acct, rv.risk.now(), config, rv.service._accounting, persist=False)


# ------------------------------------------------------------------ approval
def test_approval_reserves_exactly_what_the_in_transaction_assessment_permits(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("RSVA")
    before = ledger_entries(rv, account)
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "approve-0001")
    assert res.decision is APPROVED
    assert res.created
    # 6B vector: 92 shares, loss 499.45; notional at the adverse entry fill 92 x 99.874475
    # rounded up = 9188.46; capital = 92 x (99.874475 + 0.00998745 fees + 0.005) + 1.00 min.
    assert res.approved_quantity == D("92")
    assert res.proposed_quantity is None
    r = rv.get(res.reservation_id)
    assert (r.approved_risk, r.approved_exposure, r.approved_capital) == (
        D("499.45"),
        D("9188.46"),
        D("9190.84"),
    )
    assert r.approved_margin == ZERO
    assert (r.remaining_quantity, r.consumed_quantity, r.released_quantity) == (D(92), 0, 0)
    assert [e.event_type for e in rv.events(res.reservation_id)] == [
        ReservationEventType.CREATED,
        ReservationEventType.APPROVED,
    ]
    with session_scope(rv.factory) as session:
        a = session.get(RiskAssessmentRecord, r.assessment_id)
        assert a is not None
        assert a.result is AssessmentResult.PASS_FOR_RESERVATION
        assert a.created_txid == r.created_txid  # assessed inside the reservation transaction
        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == r.id
            )
        )
        assert approval is not None
        assert approval.approved_quantity == D(92)
        assert approval.worst_entry_price == D("99.70")
        assert approval.valid_until == r.expires_at == a.valid_until
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == str(r.id))
        )
        assert session.scalar(
            select(RiskEventRecord.summary).where(RiskEventRecord.subject_id == str(r.id))
        )
    # Reserved capacity, not money: the ledger is untouched.
    assert ledger_entries(rv, account) == before
    state = rv.state(account)
    assert (state.active_count, state.reserved_risk, state.reserved_exposure) == (
        1,
        D("499.45"),
        D("9188.46"),
    )
    v = live_view(rv, account).view
    assert v.pending_exposure == D("9188.46")
    assert v.gross_exposure == ZERO  # nothing is in the ledger yet


def test_reductions_and_rejections_are_explained_and_proposed_is_kept(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("RSVB")
    big = rv.reserve(
        account, risk.proposal(asset, "99.70", "94.70", requested_quantity=D("500")), "reduce-0001"
    )
    assert big.decision is APPROVED
    assert (big.proposed_quantity, big.approved_quantity) == (D(500), D(92))
    assert "REQUESTED_QUANTITY_REDUCED" in codes(big)
    no_stop = rv.reserve(account, risk.proposal(asset, "99.70", None), "reject-0001")
    assert no_stop.decision is REJECTED
    assert {"ASSESSMENT_REJECTED", "MISSING_STOP"} <= codes(no_stop)
    r = rv.get(no_stop.reservation_id)
    assert r.approved_quantity == 0
    assert r.remaining_risk == 0
    assert r.expires_at == r.requested_at  # a rejection holds nothing
    expired = rv.reserve(
        account,
        risk.proposal(asset, "99.70", "94.70", expires_at=risk.now() - timedelta(minutes=1)),
        "reject-0002",
    )
    assert expired.decision is REJECTED
    assert expired.assessment_id is None
    assert codes(expired) == {"PROPOSAL_EXPIRED"}
    assert rv.state(account).active_count == 1


def test_reservations_consume_the_loss_allowance_and_release_frees_it(rv):
    """Daily loss limit 2% = 2,000.00: four 499.45 reservations fit (1,997.80); the fifth
    finds 2.20 left, too little for one share, and is rejected. Releasing one frees it."""
    risk = rv.risk
    account = risk.account()
    ids = [risk.asset(f"AL{i}", sector=f"SECTOR_{i}") for i in range(6)]
    got = [
        rv.reserve(account, risk.proposal(ids[i], "99.70", "94.70"), f"allow-{i:04d}")
        for i in range(5)
    ]
    assert [g.decision for g in got] == [APPROVED] * 4 + [REJECTED]
    assert "RISK_BUDGET" in codes(got[4]) or "BELOW_MINIMUM_QUANTITY" in codes(got[4])
    assert rv.state(account).reserved_risk == D("1997.80")
    rv.service.release(
        got[0].reservation_id,
        resolution=ReleaseResolution.NOT_SUBMITTED,
        idempotency_key="allow-release-0",
        actor="system:test-worker",
        reason="not submitted (controlled test event)",
    )
    again = rv.reserve(account, risk.proposal(ids[5], "99.70", "94.70"), "allow-0005")
    assert again.decision is APPROVED
    assert again.approved_quantity == D(92)


@pytest.mark.parametrize(
    ("limits", "binding"),
    [
        ({"daily_loss_limit_pct": "0.01", "weekly_loss_limit_pct": "0.01"}, "weekly"),
        ({"max_drawdown_pct": "0.01"}, "drawdown"),
    ],
)
def test_weekly_and_drawdown_rooms_bind_reservations(rv, limits, binding):
    """With 1% weekly loss or 1% drawdown room (1,000.00), two 499.45 trades fit."""
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(account, **limits)
    risk.reconcile(account)
    ids = [risk.asset(f"W{binding[0].upper()}{i}", sector=f"SECTOR_W{i}") for i in range(3)]
    got = [
        rv.reserve(account, risk.proposal(ids[i], "99.70", "94.70"), f"{binding}-{i:04d}")
        for i in range(3)
    ]
    assert [g.decision for g in got] == [APPROVED, APPROVED, REJECTED]
    assert rv.state(account).reserved_risk == D("998.90") <= D("1000.00")


def test_circuit_breakers_block_reservations(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("BRKR")
    held = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "breaker-0001")
    assert risk.service.engage_kill_switch(account, actor=OPERATOR, reason="halt for test")
    halted = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "breaker-0002")
    assert halted.decision is REJECTED
    assert {"ASSESSMENT_RISK_HALTED", "BREAKER_ACTIVE"} <= codes(halted)
    # Halting blocks new entries; it does not silently release what is held.
    assert rv.get(held.reservation_id).status is APPROVED


# ------------------------------------------------------------------ idempotency
def test_retries_return_the_original_result_and_conflicts_are_refused(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("IDEM")
    p = risk.proposal(asset, "99.70", "94.70")
    first = rv.reserve(account, p, "idem-key-0001")
    retry = rv.reserve(account, p, "idem-key-0001")
    assert (retry.reservation_id, retry.created, retry.decision) == (
        first.reservation_id,
        False,
        APPROVED,
    )
    assert retry.approved_quantity == first.approved_quantity
    with pytest.raises(IdempotencyConflictError):
        rv.reserve(account, risk.proposal(asset, "99.70", "95.00"), "idem-key-0001")
    # A process restart: a new service instance, same key -> the original.
    restarted = make_reservations_like(rv)
    again = restarted.reserve(account, p, "idem-key-0001")
    assert again.reservation_id == first.reservation_id
    assert not again.created
    assert rv.state(account).active_count == 1
    rv.attach(first.reservation_id)
    once = rv.fill(first.reservation_id, "10", "99.70", "dup-fill")
    twice = rv.fill(first.reservation_id, "10", "99.70", "dup-fill")  # a duplicate event
    assert once.created
    assert not twice.created
    assert rv.get(first.reservation_id).consumed_quantity == D(10)


def make_reservations_like(rv: Reservations) -> Reservations:
    from app.risk.reservations.service import ReservationService

    service = ReservationService(rv.factory, rv.risk.clock, rv.service._accounting)
    return Reservations(rv.risk, service, rv.reconciler)


def test_an_interrupted_transaction_leaves_nothing_and_the_retry_reserves_once(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("INTR")
    p = risk.proposal(asset, "99.70", "94.70")
    # The worker dies after the reservation was written but before COMMIT.
    session = rv.factory()
    try:
        rv.service.reserve_in(
            session, account, p, idempotency_key="interrupt-0001", requested_by="system:w"
        )
        session.connection().invalidate()  # connection lost: the server rolls back
    finally:
        session.close()
    with session_scope(rv.factory) as s:
        assert rv.count(s) == 0
    assert rv.state(account).reserved_risk == ZERO
    # The database is interrupted mid-transaction: the server terminates the backend.
    session = rv.factory()
    try:
        rv.service.reserve_in(
            session, account, p, idempotency_key="interrupt-0001", requested_by="system:w"
        )
        with rv.factory() as killer:
            pid = session.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar()
            killer.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            killer.commit()
        with pytest.raises(OperationalError):
            session.commit()
    finally:
        session.close()
    with session_scope(rv.factory) as s:
        assert rv.count(s) == 0
    retry = rv.reserve(account, p, "interrupt-0001")
    assert retry.created
    assert retry.decision is APPROVED
    with session_scope(rv.factory) as s:
        assert rv.count(s) == 1
    assert rv.state(account).reserved_risk == D("499.45")


def test_rollback_of_the_callers_transaction_rolls_the_reservation_back(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("ROLL")

    def reserve_then_fail() -> None:
        with session_scope(rv.factory) as session:
            rv.service.reserve_in(
                session,
                account,
                risk.proposal(asset, "99.70", "94.70"),
                idempotency_key="rollback-0001",
                requested_by="system:w",
            )
            raise RuntimeError("the caller failed after reserving")

    with pytest.raises(RuntimeError):
        reserve_then_fail()
    with session_scope(rv.factory) as s:
        assert rv.count(s) == 0
        assert s.scalar(select(func.count()).select_from(RiskReservationEventRecord)) == 0
    assert rv.state(account).last_sequence == 0


# ------------------------------------------------------------------ consumption and release
def test_partial_consumption_moves_exposure_to_the_ledger_without_double_counting(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("PART")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "partial-0001")
    with pytest.raises(DBAPIError, match="attached order"):
        rv.fill(res.reservation_id, "10", "99.70", "too-early")  # no order attached yet
    rv.attach(res.reservation_id)
    done = rv.fill(res.reservation_id, "40", "99.70", "part-1")
    assert done.status is ReservationStatus.PARTIALLY_CONSUMED
    r = rv.get(res.reservation_id)
    # 40/92 of each amount is freed, rounded down (the remainder never shrinks too much).
    assert r.remaining_quantity == D(52)
    assert r.remaining_risk == D("499.45") - D("217.15")  # floor(499.45 x 40/92)
    assert r.remaining_exposure == D("9188.46") - D("3994.98")  # floor(9188.46 x 40/92)
    risk.reconcile(account)
    v = live_view(rv, account).view
    assert v.long_exposure == D("3988.00")  # 40 x 99.70 in the ledger
    assert v.pending_exposure == r.remaining_exposure  # only the unconsumed part is reserved
    # The remainder is still sized against: a 5% single-asset cap would count both.
    with pytest.raises(ReservationError, match="over-fill"):
        rv.fill(res.reservation_id, "53", "99.70", "overfill")
    # Cancelled with confirmation: only the unconsumed remainder is released.
    rel = rv.service.release(
        res.reservation_id,
        resolution=ReleaseResolution.ORDER_CANCELLED,
        idempotency_key="partial-cancel",
        actor="system:test-execution",
        reason="cancellation confirmed (controlled test event)",
    )
    assert (rel.status, rel.consumed_quantity, rel.released_quantity) == (
        ReservationStatus.RELEASED,
        D(40),
        D(52),
    )
    r = rv.get(res.reservation_id)
    assert r.remaining_quantity == 0
    assert r.remaining_risk == 0
    assert r.approved_risk == D("499.45")  # history kept
    v = live_view(rv, account).view
    assert v.long_exposure == D("3988.00")  # the fill is not reversed by a release
    assert v.pending_exposure == ZERO
    with pytest.raises(ReservationError, match="RELEASED"):
        rv.fill(res.reservation_id, "1", "99.70", "after-release")
    types = [e.event_type for e in rv.events(res.reservation_id)]
    assert types == [
        ReservationEventType.CREATED,
        ReservationEventType.APPROVED,
        ReservationEventType.ORDER_ATTACHED,
        ReservationEventType.CONSUMED,
        ReservationEventType.RELEASED,
    ]
    assert rv.reconciler.run(account, actor="system:test").status is ReconciliationOutcome.PASSED
    assert risk.reconcile(account) == "PASSED"


def test_full_consumption_in_steps_frees_exactly_the_reservation(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("FULL")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "fullfill-001")
    rv.attach(res.reservation_id)
    for i, q in enumerate(("30", "30", "31", "1")):
        rv.fill(res.reservation_id, q, "99.70", f"full-{i}")
    r = rv.get(res.reservation_id)
    assert r.status is ReservationStatus.CONSUMED
    assert r.consumed_quantity == D(92)
    assert r.remaining_quantity == 0
    with session_scope(rv.factory) as s:
        moved = s.execute(
            select(
                func.sum(RiskReservationEventRecord.risk_amount),
                func.sum(RiskReservationEventRecord.exposure_amount),
                func.sum(RiskReservationEventRecord.capital_amount),
            ).where(
                RiskReservationEventRecord.reservation_id == r.id,
                RiskReservationEventRecord.event_type == ReservationEventType.CONSUMED,
            )
        ).one()
    # Rounding down on each partial step, exact remainder on the last: the sum is exact.
    assert tuple(moved) == (D("499.45"), D("9188.46"), D("9190.84"))
    assert rv.state(account).reserved_risk == ZERO


def test_short_reservation_reserves_short_exposure_and_margin(rv):
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(account, missing_margin_policy="USE_CONFIGURED_MINIMUM")
    risk.reconcile(account)
    stock = risk.asset("SHRT", start=50.3)  # close 50.00
    res = rv.reserve(account, risk.proposal(stock, "50.00", "52.00", SHORT), "short-0001")
    assert res.decision is APPROVED
    r = rv.get(res.reservation_id)
    assert r.direction is SHORT
    assert ZERO < r.approved_risk <= D("250.00")  # half the long budget
    # Collateral beyond proceeds: 50% of notional, rounded up.
    assert r.approved_margin >= r.approved_exposure / 2
    assert r.approved_capital >= r.approved_margin
    state = rv.state(account)
    assert state.reserved_short_exposure == r.approved_exposure
    assert state.reserved_long_exposure == ZERO
    # The opposite direction is not netted against it.
    long_same = rv.reserve(account, risk.proposal(stock, "50.00", "48.00"), "short-0002")
    assert long_same.decision is REJECTED
    assert "OPPOSITE_RESERVATION_ACTIVE" in codes(long_same)
    rv.attach(res.reservation_id)
    rv.fill(res.reservation_id, "10", "50.00", "short-fill")
    assert rv.get(res.reservation_id).consumed_quantity == D(10)


def test_concentration_limits_count_reservations(rv):
    """Sector 30% and correlated cluster 40%: reservations in the same sector or cluster
    share the limit with positions."""
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(
        account,
        daily_loss_limit_pct="0.5",
        weekly_loss_limit_pct="0.5",
        max_drawdown_pct="0.5",
        risk_per_trade_pct="0.05",
    )
    risk.reconcile(account)
    semis = [risk.asset(f"SEMI{i}", sector="SEMIS", cluster="CHIPS") for i in range(3)]
    risk.books.trade(account, "long_purchase", "held-semi", semis[0], "100", "99.70")
    risk.reconcile(account)
    # Held 100 x 99.70 = 9,970.00 in SEMIS. A asks for 100 shares (~9,987); B finds the
    # sector room left (~10,043) and is cut to it; C finds none.
    a = rv.reserve(
        account,
        risk.proposal(semis[1], "99.70", "94.70", requested_quantity=D("100")),
        "sector-0001",
    )
    b = rv.reserve(account, risk.proposal(semis[2], "99.70", "94.70"), "sector-0002")
    assert a.decision is APPROVED
    assert a.approved_quantity == D(100)
    assert b.decision is APPROVED
    assert "SECTOR_EXPOSURE" in codes(b)
    v = live_view(rv, account).view
    assert v.long_exposure == D("9970.00")
    assert v.long_exposure + v.pending_exposure <= D("30000.00")  # 30% of 100,000
    assert D("30000.00") - (v.long_exposure + v.pending_exposure) < D("99.88")
    c = rv.reserve(account, risk.proposal(semis[1], "99.70", "94.70"), "sector-0003")
    assert c.decision is REJECTED
    # Another sector, same correlation cluster: the 40% cluster limit binds.
    other = risk.asset("BANK", sector="BANKS", cluster="CHIPS")
    d = rv.reserve(account, risk.proposal(other, "99.70", "94.70"), "cluster-0001")
    assert d.decision is APPROVED
    assert "CORRELATED_EXPOSURE" in codes(d)
    v = live_view(rv, account).view
    assert v.long_exposure + v.pending_exposure <= D("40000.00")


def test_multiple_currencies_value_reservations_conservatively(rv):
    from app.accounting.enums import InstrumentSpecSource
    from app.accounting.reference import register_instrument_spec
    from app.data_engine.enums import PriceAdjustment
    from app.database.models import Asset
    from app.tests.integration.accounting_support import NOW, SPEC_FROM
    from app.tests.integration.strategy_support import make_asset, seed_bars

    risk = rv.risk
    account = risk.account()
    risk.books.rate("EUR", "1.0835")
    eur = make_asset(risk.factory, "EURRSV")
    with session_scope(risk.factory) as session:
        stored = session.get(Asset, eur)
        assert stored is not None
        stored.quote_currency = "EUR"
        register_instrument_spec(
            session,
            asset_id=eur,
            quantity_increment=D("1"),
            min_quantity=D("1"),
            valid_from=SPEC_FROM,
            source=InstrumentSpecSource.OPERATOR,
            provenance="test fixture lot size",
            created_by="test",
        )
    seed_bars(
        risk.factory, eur, as_of=NOW, count=5, start=100.0, step=0.0, adjustment=PriceAdjustment.RAW
    )
    risk.adv(eur, "50000000")
    res = rv.reserve(account, risk.proposal(eur, "99.70", "94.70"), "eur-reserve-1")
    assert res.decision is APPROVED
    r = rv.get(res.reservation_id)
    assert r.quote_currency == "EUR"
    assert r.functional_currency == "USD"
    assert r.fx_rate == D("1.0835")
    assert r.approved_risk <= D("500.00")
    base = live_view(rv, account).view.reservations[0].exposure
    assert base == r.approved_exposure
    # EUR strengthens: the reserved exposure is re-valued up (never down).
    risk.clock.advance(timedelta(minutes=1))
    risk.books.rate("EUR", "1.2000")
    up = live_view(rv, account).view.reservations[0]
    assert up.exposure > base
    assert up.risk > r.approved_risk
    risk.clock.advance(timedelta(minutes=1))
    risk.books.rate("EUR", "1.0000")  # weakens: the approval-time value stays
    down = live_view(rv, account).view.reservations[0]
    assert down.exposure == base
    # No fresh rate at all: the reservation cannot be valued, so nothing new is approved.
    risk.clock.advance(timedelta(hours=97))  # past the 96-hour freshness window
    view = live_view(rv, account).view
    assert view.reservation_issues
    assert view.reservation_issues[0]["currency"] == "EUR"


def test_identity_and_duplicate_intents_are_refused(rv):
    from app.tests.integration.ai_support import make_proposal, make_strategy

    risk = rv.risk
    account = risk.account()
    asset = risk.asset("IDNT")
    from app.risk import proposals
    from app.strategies.enums import StrategyFamily

    version = make_strategy(risk.factory, "momentum_fixture", StrategyFamily.MOMENTUM)
    pid = make_proposal(
        risk.factory,
        strategy_id="momentum_fixture",
        version_id=version,
        asset_id=asset,
        stored_at=risk.now() - timedelta(hours=1),
        cutoff=risk.now() - timedelta(hours=2),
        entry="99.70",
        expires_at=risk.now() + timedelta(days=1),
    )

    with session_scope(risk.factory) as session:
        p = proposals.from_strategy_proposal(session, pid)
    first = rv.reserve(account, p, "intent-0001")
    assert first.decision is APPROVED
    dup = rv.reserve(account, p, "intent-0002")  # same intent, another key
    assert dup.decision is REJECTED
    assert "DUPLICATE_ACTIVE_RESERVATION" in codes(dup)
    forged = p.model_copy(update={"stop": D("90.00")})
    bad = rv.reserve(account, forged, "intent-0003")
    assert "PROPOSAL_IDENTITY" in codes(bad)


# ------------------------------------------------------------------ expiry and uncertainty
def test_expiry_releases_only_reservations_nothing_depends_on(rv):
    risk = rv.risk
    account = risk.account()
    a, b = risk.asset("EXPA", sector="S_A"), risk.asset("EXPB", sector="S_B")
    free = rv.reserve(account, risk.proposal(a, "99.70", "94.70"), "expiry-0001")
    linked = rv.reserve(account, risk.proposal(b, "99.70", "94.70"), "expiry-0002")
    rv.attach(linked.reservation_id, "order-b")
    assert rv.service.expire_due(account).expired == []  # not due yet
    risk.clock.advance(timedelta(seconds=301))
    with pytest.raises(DBAPIError, match="expired"):
        rv.attach(free.reservation_id, "order-late")
    summary = rv.service.expire_due(account)
    assert summary.expired == [free.reservation_id]
    assert summary.deferred == [linked.reservation_id]
    assert rv.service.expire_due(account).deferred == []  # deferred once, then just held
    assert rv.get(free.reservation_id).status is ReservationStatus.EXPIRED
    held = rv.get(linked.reservation_id)
    assert held.status is APPROVED
    assert held.expiry_deferred_at is not None
    assert rv.state(account).reserved_risk == D("499.45")  # the linked one keeps capacity
    recon = rv.reconciler.run(account, actor="system:test")
    assert recon.status is ReconciliationOutcome.PASSED
    assert {w["code"] for w in recon.warnings} == {"HELD_PAST_EXPIRY"}


def test_uncertain_execution_is_never_released_automatically(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("UNCR")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "uncertain-001")
    rv.service.flag_uncertain(
        res.reservation_id,
        idempotency_key="uncertain-flag",
        actor="system:test-worker",
        reason="worker crashed during submission (controlled test event)",
    )
    risk.clock.advance(timedelta(hours=1))
    summary = rv.service.expire_due(account)
    assert summary.expired == []
    assert summary.deferred == [res.reservation_id]
    with pytest.raises(ReservationError, match=r"uncertain|review"):
        rv.service.release(
            res.reservation_id,
            resolution=ReleaseResolution.NOT_SUBMITTED,
            idempotency_key="uncertain-rel1",
            actor="system:test-worker",
            reason="give it back",
        )
    with pytest.raises(ReservationError, match="operator"):
        rv.service.fail(
            res.reservation_id, idempotency_key="uncertain-fail", actor="system:w", reason="timeout"
        )
    assert rv.get(res.reservation_id).execution_link is ExecutionLink.UNCERTAIN
    rv.service.release(
        res.reservation_id,
        resolution=ReleaseResolution.OPERATOR,
        idempotency_key="uncertain-rel2",
        actor=OPERATOR,
        reason="broker confirms no order exists (reviewed)",
    )
    assert rv.get(res.reservation_id).status is ReservationStatus.RELEASED


def test_confirmed_failure_releases_the_remainder(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("FAIL")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "failure-0001")
    out = rv.service.fail(
        res.reservation_id,
        idempotency_key="failure-fail",
        actor="system:test-execution",
        reason="order construction failed before submission",
    )
    assert out.status is ReservationStatus.FAILED
    assert rv.state(account).active_count == 0


# ------------------------------------------------------------------ database guards
def test_the_database_refuses_bypassing_the_reservation_protocol(rv, fresh_engine):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("GRD")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "guard-key-001")
    rid = res.reservation_id

    def refused(sql: str, params: dict[str, object], match: str) -> None:
        with pytest.raises(DBAPIError, match=match), fresh_engine.begin() as conn:
            conn.execute(text(sql), params)

    refused(
        "UPDATE risk_reservations SET remaining_risk = 0 WHERE id = :r", {"r": rid}, "projection"
    )
    refused("DELETE FROM risk_reservations WHERE id = :r", {"r": rid}, "append-only")
    refused("UPDATE risk_reservation_states SET reserved_risk = 0", {}, "changes only")
    refused("DELETE FROM risk_reservation_events", {}, "append-only")
    refused("TRUNCATE risk_reservation_events CASCADE", {}, "append-only")
    refused("UPDATE risk_reservation_approvals SET approved_quantity = 1000", {}, "append-only")
    state = rv.state(account)
    # An approval not computed by a passing assessment of the same transaction.
    refused(
        "INSERT INTO risk_reservations (portfolio_account_id, idempotency_key, request_sha256, "
        "source, assessment_id, asset_id, symbol, asset_class, direction, quote_currency, "
        "functional_currency, entry, status, expires_at, requested_by, requested_at) "
        "SELECT portfolio_account_id, 'forged-key-1', request_sha256, source, assessment_id, "
        "asset_id, symbol, asset_class, direction, quote_currency, functional_currency, entry, "
        "'PENDING', expires_at, 'rogue', requested_at FROM risk_reservations WHERE id = :r",
        {"r": rid},
        "its own transaction",
    )
    # A consumption without a matching ledger fill.
    rv.attach(rid)
    with session_scope(rv.factory) as s:
        funding = s.scalar(
            select(JournalEntryRecord.id).where(JournalEntryRecord.portfolio_account_id == account)
        )
    refused(
        "INSERT INTO risk_reservation_events (reservation_id, portfolio_account_id, "
        "account_sequence, reservation_sequence, event_type, from_status, to_status, quantity, "
        "journal_entry_id, order_ref, actor, reason, occurred_at) VALUES (:r, :a, :n, 4, "
        "'CONSUMED', 'APPROVED', 'PARTIALLY_CONSUMED', 10, :j, 'test-order', 'rogue', "
        "'fake fill', now())",
        {"r": rid, "a": account, "n": state.last_sequence + 2, "j": funding},
        "does not move",
    )
    # A transition the state machine does not allow (APPROVED -> APPROVED again).
    refused(
        "INSERT INTO risk_reservation_events (reservation_id, portfolio_account_id, "
        "account_sequence, reservation_sequence, event_type, from_status, to_status, quantity, "
        "risk_amount, exposure_amount, actor, reason, occurred_at) VALUES (:r, :a, :n, 4, "
        "'APPROVED', 'APPROVED', 'APPROVED', 1, 1, 1, 'rogue', 'again', now())",
        {"r": rid, "a": account, "n": state.last_sequence + 2},
        "PENDING",
    )
    # Expiry while an order depends on it.
    refused(
        "INSERT INTO risk_reservation_events (reservation_id, portfolio_account_id, "
        "account_sequence, reservation_sequence, event_type, from_status, to_status, quantity, "
        "risk_amount, exposure_amount, capital_amount, actor, reason, occurred_at) "
        "SELECT id, portfolio_account_id, :n, 4, 'EXPIRED', 'APPROVED', 'EXPIRED', "
        "remaining_quantity, remaining_risk, remaining_exposure, remaining_capital, 'rogue', "
        "'expire', expires_at + interval '1 day' FROM risk_reservations WHERE id = :r",
        {"r": rid, "n": state.last_sequence + 2},
        "cannot expire",
    )
    assert rv.reconciler.run(account, actor="system:test").status is ReconciliationOutcome.PASSED


def test_reconciliation_detects_tampering_and_blocks_new_approvals(rv, fresh_engine):
    risk = rv.risk
    account = risk.account()
    a, b = risk.asset("TMPA", sector="S_A"), risk.asset("TMPB", sector="S_B")
    res = rv.reserve(account, risk.proposal(a, "99.70", "94.70"), "tamper-00001")
    assert rv.reconciler.run(account, actor="system:test").status is ReconciliationOutcome.PASSED
    # Someone with owner rights bypasses the triggers and frees capacity silently.
    with fresh_engine.begin() as conn:
        conn.execute(text("ALTER TABLE risk_reservations DISABLE TRIGGER USER"))
        conn.execute(
            text("UPDATE risk_reservations SET remaining_risk = 0 WHERE id = :r"),
            {"r": res.reservation_id},
        )
        conn.execute(text("ALTER TABLE risk_reservations ENABLE TRIGGER USER"))
    recon = rv.reconciler.run(account, actor="system:test")
    assert recon.status is ReconciliationOutcome.FAILED
    found = {(f["code"], f.get("field")) for f in recon.unexplained}
    assert ("PROJECTION_MISMATCH", "remaining_risk") in found
    assert ("TOTALS_MISMATCH", "reserved_risk") in found
    with session_scope(rv.factory) as s:
        health = s.scalar(select(SystemHealthEventRecord))
        assert health is not None
        assert health.severity.value == "CRITICAL"
    blocked = rv.reserve(account, risk.proposal(b, "99.70", "94.70"), "tamper-00002")
    assert blocked.decision is REJECTED
    assert "RESERVATION_RECONCILIATION_FAILED" in codes(blocked)
    # The database refuses an approval too, whatever the caller (checked by the trigger).
    with session_scope(rv.factory) as s:
        r = s.get(RiskReservationRecord, res.reservation_id)
        assert r is not None
        assert r.status is APPROVED


def test_recovery_after_a_worker_crash(rv):
    """A worker reserved and died. On restart: an orphan with no order expires when due; one
    with an order is held (never released because a worker timed out); the reconciliation
    passes and nothing is reserved twice when the worker retries its key."""
    risk = rv.risk
    account = risk.account()
    a, b = risk.asset("RCVA", sector="S_A"), risk.asset("RCVB", sector="S_B")
    orphan = rv.reserve(account, risk.proposal(a, "99.70", "94.70"), "recover-0001")
    linked = rv.reserve(account, risk.proposal(b, "99.70", "94.70"), "recover-0002")
    rv.attach(linked.reservation_id, "order-in-flight")
    risk.clock.advance(timedelta(minutes=10))
    restarted = make_reservations_like(rv)
    summary = restarted.service.expire_due(account)
    assert summary.expired == [orphan.reservation_id]
    assert summary.deferred == [linked.reservation_id]
    retry = restarted.reserve(account, risk.proposal(a, "99.70", "94.70"), "recover-0001")
    assert not retry.created
    assert retry.status is ReservationStatus.EXPIRED
    assert retry.decision is APPROVED  # the original decision is reported
    assert (
        rv.reconciler.run(account, actor="system:recovery").status is ReconciliationOutcome.PASSED
    )
    assert rv.state(account).active_count == 1

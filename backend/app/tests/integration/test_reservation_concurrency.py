"""Phase 6C concurrency and financial stress tests against a real PostgreSQL server.

Many threads, each with its own connection, hit the same portfolio account at once. Every
worker first runs its *own* risk assessment (the Phase 6B pure check, which reserves
nothing) and sees enough capacity; only the transactional reservation may decide the final
allocation. After each run the database is checked against independent sums: no limit is
exceeded, nothing is reserved twice, totals equal the active reservations, and the
reservation reconciliation passes. TEST FIXTURE data only; nothing is executed.
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from app.accounting.money import ZERO
from app.database.models import JournalEntryRecord
from app.database.models.reservations import (
    RiskReservationEventRecord,
    RiskReservationRecord,
    RiskReservationRequestRecord,
    RiskReservationStateRecord,
)
from app.database.sessions import session_scope
from app.risk.enums import (
    ACTIVE_RESERVATION_STATUSES,
    AssessmentResult,
    ReconciliationOutcome,
    ReleaseResolution,
    ReservationStatus,
    TradeDirection,
)
from app.risk.reservations.service import ReservationError
from app.tests.integration.reservation_support import Reservations, make_reservations

pytestmark = pytest.mark.integration
PASS = AssessmentResult.PASS_FOR_RESERVATION


@pytest.fixture
def wide_engine(fresh_engine: Engine) -> Iterator[Engine]:
    """The test database with enough pooled connections for every thread at once."""
    engine = create_engine(
        fresh_engine.url.render_as_string(hide_password=False),
        pool_size=64,
        max_overflow=0,
        pool_timeout=120,
        connect_args={"options": "-c timezone=UTC"},
    )
    yield engine
    engine.dispose()


@pytest.fixture
def rv(wide_engine: Engine) -> Reservations:
    return make_reservations(wide_engine)


def run_threads(count: int, work: Callable[[int], Any]) -> tuple[list[Any], list[BaseException]]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count
    errors: list[BaseException] = []
    lock = threading.Lock()

    def target(i: int) -> None:
        try:
            barrier.wait()
            results[i] = work(i)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
    return results, errors


def assets(rv: Reservations, n: int, prefix: str = "CC") -> list[uuid.UUID]:
    """Fixture assets (MOCK close 99.70) in distinct sectors."""
    return [rv.risk.asset(f"{prefix}{i:02d}", sector=f"SECTOR_{prefix}{i:02d}") for i in range(n)]


def check_invariants(rv: Reservations, account: uuid.UUID, limits: dict[str, D]) -> dict[str, Any]:
    """Independent sums over the stored rows (not the trigger-maintained totals)."""
    with session_scope(rv.factory) as session:
        rows = session.scalars(
            select(RiskReservationRecord).where(
                RiskReservationRecord.portfolio_account_id == account
            )
        ).all()
        active = [r for r in rows if r.status in ACTIVE_RESERVATION_STATUSES]
        per_asset: dict[uuid.UUID, D] = defaultdict(lambda: ZERO)
        for r in active:
            per_asset[r.asset_id] += r.remaining_exposure
        sums = {
            "risk": sum((r.remaining_risk for r in active), ZERO),
            "exposure": sum((r.remaining_exposure for r in active), ZERO),
            "capital": sum((r.remaining_capital for r in active), ZERO),
            "margin": sum((r.remaining_margin for r in active), ZERO),
        }
        state = session.get(RiskReservationStateRecord, account)
        assert state is not None
        # The trigger-maintained totals equal the independent sums.
        assert state.active_count == len(active)
        assert state.reserved_risk == sums["risk"]
        assert state.reserved_exposure == sums["exposure"]
        assert state.reserved_capital == sums["capital"]
        assert state.reserved_margin == sums["margin"]
        # Gapless event numbering: every event was computed from the latest state.
        seqs = session.scalars(
            select(RiskReservationEventRecord.account_sequence)
            .where(RiskReservationEventRecord.portfolio_account_id == account)
            .order_by(RiskReservationEventRecord.account_sequence)
        ).all()
        assert list(seqs) == list(range(1, len(seqs) + 1))
        assert state.last_sequence == len(seqs)
        # No idempotency key twice; one RESERVE record per reservation.
        keys = [r.idempotency_key for r in rows]
        assert len(keys) == len(set(keys))
        reserve_requests = session.scalar(
            select(func.count())
            .select_from(RiskReservationRequestRecord)
            .where(
                RiskReservationRequestRecord.portfolio_account_id == account,
                RiskReservationRequestRecord.command == "RESERVE",
            )
        )
        assert reserve_requests == len(rows)
        # Reservations never post to the ledger.
        assert session.scalar(
            select(func.count())
            .select_from(JournalEntryRecord)
            .where(JournalEntryRecord.portfolio_account_id == account)
        ) == limits.get("journal_entries", 1)
    if "risk" in limits:
        assert sums["risk"] <= limits["risk"]
    if "exposure" in limits:
        assert sums["exposure"] <= limits["exposure"]
    if "capital" in limits:
        assert sums["capital"] <= limits["capital"]
    if "single_asset" in limits:
        assert max(per_asset.values(), default=ZERO) <= limits["single_asset"]
    if "positions" in limits:
        assert len(per_asset) <= limits["positions"]
    recon = rv.reconciler.run(account, actor="system:test")
    assert recon.status is ReconciliationOutcome.PASSED, recon.unexplained
    return {"active": active, "sums": sums, "per_asset": per_asset, "rows": rows}


# ------------------------------------------------------------------ over-allocation
def test_concurrent_workers_cannot_overspend_the_loss_allowance(rv):
    """Default limits: the 2,000.00 daily loss allowance fits four 499.45-risk trades.
    Thirty workers each assess first (all PASS: each sees the whole allowance), then all
    reserve at once. Exactly four are approved; the rest are rejected and explained."""
    risk = rv.risk
    account = risk.account()
    ids = assets(rv, 10)
    workers = 30

    def proposal(i: int) -> Any:
        return risk.proposal(ids[i % len(ids)], "99.70", "94.70")

    # Adversarial precondition: every worker independently "sees" enough capacity.
    pre = [
        risk.service.assess(account, proposal(i), requested_by="system:worker")
        for i in range(workers)
    ]
    assert all(p.result is PASS for p in pre)
    naive = sum((p.outcome.potential_loss for p in pre if p.outcome), ZERO)
    assert naive > D("2000")  # trusting those would allocate 30 x 499.45

    results, errors = run_threads(
        workers, lambda i: rv.reserve(account, proposal(i), f"budget-{i:03d}", f"system:worker-{i}")
    )
    assert errors == []
    decisions = Counter(r.decision for r in results)
    assert decisions[ReservationStatus.APPROVED] == 4
    assert decisions[ReservationStatus.REJECTED] == workers - 4
    for r in results:
        if r.decision is ReservationStatus.REJECTED:
            assert r.reasons  # every rejection is explained
            assert r.approved_quantity == 0
    out = check_invariants(rv, account, {"risk": D("2000.00"), "positions": D(8)})
    assert out["sums"]["risk"] == D("1997.80")


def test_concurrent_workers_cannot_exceed_gross_exposure_or_capital(rv):
    """Loss limits loosened so exposure binds: risk 2% per trade sizes each trade up to the
    20% single-asset limit (200 shares, ~19,975); gross is limited to 100% of equity and
    capital to the cash. Forty workers on twelve assets."""
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(
        account,
        risk_per_trade_pct="0.02",
        daily_loss_limit_pct="0.5",
        weekly_loss_limit_pct="0.5",
        max_drawdown_pct="0.5",
        max_correlated_exposure_pct="1",
        max_open_positions=50,
    )
    risk.reconcile(account)
    ids = assets(rv, 12, "GX")
    workers = 40

    def proposal(i: int) -> Any:
        return risk.proposal(ids[i % len(ids)], "99.70", "94.70")

    pre = [
        risk.service.assess(account, proposal(i), requested_by="system:worker") for i in range(12)
    ]
    assert all(p.result is PASS for p in pre)
    assert sum((p.outcome.notional for p in pre if p.outcome), ZERO) > D("100000")

    results, errors = run_threads(
        workers, lambda i: rv.reserve(account, proposal(i), f"gross-{i:03d}", f"system:worker-{i}")
    )
    assert errors == []
    approved = [r for r in results if r.decision is ReservationStatus.APPROVED]
    assert len(approved) >= 5
    out = check_invariants(
        rv,
        account,
        {"exposure": D("100000.00"), "capital": D("100000.00"), "single_asset": D("20000.00")},
    )
    # The capacity is used, not wasted: what is left cannot fit one more share.
    assert D("100000.00") - out["sums"]["exposure"] < D("100")


def test_concurrent_workers_respect_the_position_count(rv):
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(account, max_open_positions=3)
    risk.reconcile(account)
    ids = assets(rv, 9, "PC")
    workers = 27
    results, errors = run_threads(
        workers,
        lambda i: rv.reserve(
            account,
            risk.proposal(ids[i % 9], "99.70", "94.70", requested_quantity=D("5")),
            f"count-{i:03d}",
        ),
    )
    assert errors == []
    approved = {r.reservation_id for r in results if r.decision is ReservationStatus.APPROVED}
    out = check_invariants(rv, account, {"positions": D(3)})
    assert len(out["per_asset"]) == 3
    assert len(approved) >= 3


def test_concurrent_workers_on_one_asset_respect_the_single_asset_limit(rv):
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(
        account,
        risk_per_trade_pct="0.05",
        daily_loss_limit_pct="0.5",
        weekly_loss_limit_pct="0.5",
        max_drawdown_pct="0.5",
    )
    risk.reconcile(account)
    asset = risk.asset("ONE")
    results, errors = run_threads(
        24,
        lambda i: rv.reserve(
            account,
            risk.proposal(asset, "99.70", "94.70", requested_quantity=D("30")),
            f"single-{i:03d}",
        ),
    )
    assert errors == []
    reduced = [
        r for r in results if r.decision is ReservationStatus.APPROVED and r.approved_quantity < 30
    ]
    for r in reduced:  # every reduction is explained
        assert {x["code"] for x in r.reasons} & {
            "SINGLE_ASSET_EXPOSURE",
            "REQUESTED_QUANTITY_REDUCED",
        }
    out = check_invariants(rv, account, {"single_asset": D("20000.00")})
    # 20% of 100,000 at ~99.87 a share: 200 shares at most, and no slack of a share left.
    total = sum((r.approved_quantity for r in out["active"]), ZERO)
    assert total <= 200
    assert D("20000.00") - out["sums"]["exposure"] < D("99.88")


def test_concurrent_long_and_short_reservations(rv):
    """Shorts reserve short exposure and margin; longs and shorts on other assets compete
    for the same gross limit; the 30% short limit holds."""
    risk = rv.risk
    account = risk.account(configure=False)
    risk.configure(
        account,
        risk_per_trade_pct="0.02",
        daily_loss_limit_pct="0.5",
        weekly_loss_limit_pct="0.5",
        max_drawdown_pct="0.5",
        max_correlated_exposure_pct="1",
        missing_margin_policy="USE_CONFIGURED_MINIMUM",
        max_open_positions=50,
    )
    risk.reconcile(account)
    longs = assets(rv, 6, "LG")
    shorts = [risk.asset(f"SH{i:02d}", start=50.3, sector=f"SECTOR_SH{i:02d}") for i in range(6)]

    def work(i: int) -> Any:
        if i % 2:
            p = risk.proposal(shorts[i % 6], "50.00", "52.00", TradeDirection.SHORT)
        else:
            p = risk.proposal(longs[i % 6], "99.70", "94.70")
        return rv.reserve(account, p, f"longshort-{i:03d}")

    _results, errors = run_threads(24, work)
    assert errors == []
    out = check_invariants(rv, account, {"exposure": D("100000.00"), "capital": D("100000.00")})
    short_total = sum(
        (r.remaining_exposure for r in out["active"] if r.direction is TradeDirection.SHORT), ZERO
    )
    assert ZERO < short_total <= D("30000.00")
    margin = sum((r.remaining_margin for r in out["active"]), ZERO)
    assert margin == sum(
        (r.remaining_margin for r in out["active"] if r.direction is TradeDirection.SHORT), ZERO
    )
    assert ZERO < margin <= D("50000.00")


# ------------------------------------------------------------------ idempotency
def test_concurrent_retries_of_one_request_create_one_reservation(rv):
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("RTRY")
    results, errors = run_threads(
        20, lambda i: rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "same-key-0001")
    )
    assert errors == []
    assert len({r.reservation_id for r in results}) == 1
    assert sum(1 for r in results if r.created) == 1
    assert all(r.decision is ReservationStatus.APPROVED for r in results)
    out = check_invariants(rv, account, {})
    assert len(out["rows"]) == 1
    assert out["sums"]["risk"] == D("499.45")  # capacity used once


def test_concurrent_mixed_new_requests_and_retries(rv):
    """Twelve keys, each sent by three workers at once (worker and client retries)."""
    risk = rv.risk
    account = risk.account()
    ids = assets(rv, 12, "MX")
    results, errors = run_threads(
        36,
        lambda i: rv.reserve(
            account,
            risk.proposal(ids[i % 12], "99.70", "94.70", requested_quantity=D("10")),
            f"mixed-{i % 12:03d}",
        ),
    )
    assert errors == []
    by_key: dict[int, set[uuid.UUID]] = defaultdict(set)
    for i, r in enumerate(results):
        by_key[i % 12].add(r.reservation_id)
    assert all(len(v) == 1 for v in by_key.values())
    out = check_invariants(rv, account, {"risk": D("2000.00"), "positions": D(8)})
    assert len(out["rows"]) == 12


# ------------------------------------------------------------------ consumption and release
def test_concurrent_fills_and_a_release_never_over_consume(rv):
    """One 92-share reservation; 30 workers each deliver a 5-share controlled test fill
    (some twice: duplicate events), while another confirms the order's cancellation.
    Whatever the interleaving: consumed + released = approved, the ledger holds exactly the
    consumed quantity, and no fill beyond the reservation is accepted."""
    risk = rv.risk
    account = risk.account()
    asset = risk.asset("FILL")
    res = rv.reserve(account, risk.proposal(asset, "99.70", "94.70"), "fill-reserve-01")
    assert res.approved_quantity == D("92")
    rv.attach(res.reservation_id)

    def work(i: int) -> Any:
        if i == 30:
            try:
                return rv.service.release(
                    res.reservation_id,
                    resolution=ReleaseResolution.ORDER_CANCELLED,
                    idempotency_key="cancel-confirmed-1",
                    actor="system:test-execution",
                    reason="cancellation confirmed (controlled test event)",
                )
            except ReservationError as exc:
                return exc
        try:
            return rv.fill(res.reservation_id, "5", "99.70", f"f{i % 24:03d}")
        except (ReservationError, Exception) as exc:  # a fill after the release is refused
            return exc

    _results, errors = run_threads(31, work)
    assert errors == []
    r = rv.get(res.reservation_id)
    assert r.consumed_quantity + r.released_quantity + r.remaining_quantity == D("92")
    assert r.consumed_quantity % 5 == 0
    assert r.consumed_quantity <= D("92")
    with session_scope(rv.factory) as session:
        held = session.execute(
            text(
                "SELECT COALESCE(SUM(quantity_delta), 0) FROM asset_quantity_ledger "
                "WHERE portfolio_account_id = :a AND asset_id = :s"
            ),
            {"a": account, "s": asset},
        ).scalar_one()
        consumed_events = session.scalar(
            select(func.count())
            .select_from(RiskReservationEventRecord)
            .where(
                RiskReservationEventRecord.reservation_id == res.reservation_id,
                RiskReservationEventRecord.event_type == "CONSUMED",
            )
        )
        fills = session.scalar(
            select(func.count())
            .select_from(JournalEntryRecord)
            .where(
                JournalEntryRecord.portfolio_account_id == account,
                JournalEntryRecord.entry_type == "LONG_PURCHASE",
            )
        )
    # Exposure moved, never duplicated: ledger quantity = consumed = 5 x consumption events.
    assert consumed_events is not None
    assert fills is not None
    assert D(held) == r.consumed_quantity == 5 * consumed_events
    assert fills == consumed_events  # a refused fill rolled its posting back with it
    assert r.status in (ReservationStatus.RELEASED, ReservationStatus.CONSUMED)
    check_invariants(rv, account, {"journal_entries": D(1 + fills)})
    assert risk.reconcile(account) == "PASSED"  # the 6A ledger reconciles too


def test_concurrent_reserve_release_and_expiry(rv):
    """Reservations, releases and expiry sweeps interleaved on one account."""
    risk = rv.risk
    account = risk.account()
    ids = assets(rv, 6, "RX")
    first = [
        rv.reserve(
            account,
            risk.proposal(ids[i], "99.70", "94.70", requested_quantity=D("20")),
            f"rx-first-{i:03d}",
        )
        for i in range(3)
    ]

    def work(i: int) -> Any:
        if i < 3:
            return rv.service.release(
                first[i].reservation_id,
                resolution=ReleaseResolution.NOT_SUBMITTED,
                idempotency_key=f"rx-release-{i:03d}",
                actor="system:test-worker",
                reason="the worker decided not to submit (controlled test event)",
            )
        if i < 6:
            return rv.service.expire_due(account)
        return rv.reserve(
            account,
            risk.proposal(ids[i % 6], "99.70", "94.70", requested_quantity=D("20")),
            f"rx-new-{i:03d}",
        )

    _, errors = run_threads(18, work)
    assert errors == []
    out = check_invariants(rv, account, {"risk": D("2000.00"), "positions": D(8)})
    statuses = Counter(r.status for r in out["rows"])
    assert statuses[ReservationStatus.RELEASED] == 3


# ------------------------------------------------------------------ the database backstop
def test_a_stale_event_is_refused_even_without_the_account_lock(rv, wide_engine):
    """Two transactions that skip the service (and its account lock) each write an event for
    a *different* reservation, both numbered from the same account state. The row locks do
    not collide, but the account counter does: the second is refused with SQLSTATE 40001."""
    risk = rv.risk
    account = risk.account()
    a, b = risk.asset("BKSA", sector="S_A"), risk.asset("BKSB", sector="S_B")
    first_res = rv.reserve(account, risk.proposal(a, "99.70", "94.70"), "backstop-0001")
    second_res = rv.reserve(account, risk.proposal(b, "99.70", "94.70"), "backstop-0002")
    state = rv.state(account)
    sql = text(
        "INSERT INTO risk_reservation_events (reservation_id, portfolio_account_id, "
        "account_sequence, reservation_sequence, event_type, from_status, to_status, actor, "
        "reason, occurred_at) VALUES (:r, :a, :n, 3, 'EXECUTION_UNCERTAIN', 'APPROVED', "
        "'APPROVED', 'system:rogue', 'rogue writer', now())"
    )
    n = state.last_sequence + 1
    c1, c2 = wide_engine.connect(), wide_engine.connect()
    outcome: list[BaseException] = []
    try:
        t1 = c1.begin()
        c1.execute(sql, {"r": first_res.reservation_id, "a": account, "n": n})

        def other() -> None:
            try:
                with c2.begin():
                    c2.execute(sql, {"r": second_res.reservation_id, "a": account, "n": n})
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=2)  # it waits for the first transaction's counter row
        assert thread.is_alive()
        t1.commit()
        thread.join(timeout=60)
    finally:
        c1.close()
        c2.close()
    assert len(outcome) == 1
    assert isinstance(outcome[0], DBAPIError)
    assert getattr(outcome[0].orig, "sqlstate", None) == "40001"
    assert rv.get(second_res.reservation_id).execution_link.value == "NONE"
    assert rv.state(account).last_sequence == n

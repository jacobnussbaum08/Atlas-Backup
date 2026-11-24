"""Phase 7B automated position management on a real PostgreSQL (TEST FIXTURE market data).

Positions are simulated positions of CONTROLLED_TEST accounts; quotes, trades and bars are
invented MOCK observations; every exit is a simulated paper order filled by the isolated
paper broker. Checks: take-profit, stop-loss, one-cancels-other, stop-first on ambiguous
bars, trailing stops, expiry, close requests, entry-remainder withdrawal, shorts with
borrow, circuit breakers and stale data, each with the 6A ledger reconciling.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app.database.models.accounting import AssetQuantityRecord
from app.database.models.execution import OrderRecord
from app.database.sessions import session_scope
from app.execution.enums import ExitReason, OrderPurpose, OrderType
from app.risk import governance
from app.risk.enums import TradeDirection
from app.tests.integration.simulation_support import SESSION, D, Sim, make_simulation

pytestmark = pytest.mark.integration


@pytest.fixture
def sim(fresh_engine) -> Sim:
    return make_simulation(fresh_engine)


def open_long(
    sim: Sim,
    symbol: str,
    *,
    quantity: str = "10",
    target: str | None = None,
    ask_size: str = "100",
):
    """A filled long market entry (bought at 99.58 for 10 of 100 displayed)."""
    ex = sim.ex
    account = ex.account(f"pm-{symbol.lower()}")
    asset = ex.asset(symbol)
    extra: dict[str, Any] = {} if target is None else {"target": D(target)}
    r = ex.reservation(account, asset, f"pm-res-{symbol}", **extra)
    entry = ex.open_order(account, r, quantity, f"pm-order-{symbol}")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50), ask_size=ask_size)
    sim.simulate()
    ex.dispatch()
    return account, asset, r, entry


def exits(sim: Sim, entry) -> list[OrderRecord]:
    with session_scope(sim.factory) as session:
        rows = session.scalars(
            select(OrderRecord)
            .where(OrderRecord.parent_order_id == entry, OrderRecord.purpose == OrderPurpose.EXIT)
            .order_by(OrderRecord.submitted_at, OrderRecord.id)
        ).all()
        session.expunge_all()
        return list(rows)


def ledger_quantity(sim: Sim, account, asset) -> D:
    with session_scope(sim.factory) as session:
        return D(
            session.scalar(  # type: ignore[arg-type]  # coalesce: never NULL
                select(func.coalesce(func.sum(AssetQuantityRecord.quantity_delta), 0)).where(
                    AssetQuantityRecord.portfolio_account_id == account,
                    AssetQuantityRecord.asset_id == asset,
                )
            )
        )


def ledger_realized(sim: Sim, account, asset) -> D:
    with session_scope(sim.factory) as session:
        return D(
            session.scalar(  # type: ignore[arg-type]  # coalesce: never NULL
                select(func.coalesce(func.sum(AssetQuantityRecord.realized_pnl), 0)).where(
                    AssetQuantityRecord.portfolio_account_id == account,
                    AssetQuantityRecord.asset_id == asset,
                )
            )
        )


def reconciled(sim: Sim, account) -> str:
    sim.ex.dispatch()
    status: str = sim.ex.risk.reconcile(account)
    return status


# ---------------------------------------------------------------------- take-profit
def test_a_resting_take_profit_fills_only_through_the_target_and_closes(sim):
    account, asset, _r, entry = open_long(sim, "PMT", target="101.00")
    sim.monitor_once()
    (tp,) = exits(sim, entry)
    assert (tp.order_type, tp.exit_reason, tp.limit_price, tp.quantity) == (
        OrderType.TAKE_PROFIT,
        ExitReason.TAKE_PROFIT,
        D("101.00"),
        D(10),
    )
    sim.ex.dispatch()
    sim.later(30)
    sim.trade(asset, "101.00", "500")  # a touch: not a fill
    sim.later(1)
    sim.simulate()
    assert sim.ex.order(tp.id).filled_quantity == D(0)
    sim.trade(asset, "101.02", "200")
    sim.later(1)
    sim.simulate()
    assert sim.ex.order(tp.id).status.value == "FILLED"
    position = sim.position(entry)
    assert position.status.value == "CLOSED"
    assert position.exit_reason is ExitReason.TAKE_PROFIT
    assert position.realized_pnl == D("14.20")  # (101.00 - 99.58) x 10
    assert position.realized_pnl == ledger_realized(sim, account, asset)
    assert ledger_quantity(sim, account, asset) == 0
    # Commission 1.00 per order + sell fees (SEC fee and TAF, rounded up to a cent).
    assert position.commissions == D("2.00")
    assert position.fees == D("0.04")
    assert reconciled(sim, account) == "PASSED"


# ---------------------------------------------------------------------- stop-loss + OCO
def test_a_stop_loss_cancels_the_take_profit_then_exits_at_the_market(sim):
    account, asset, r, entry = open_long(sim, "PMS", target="101.00")
    sim.monitor_once()
    sim.ex.dispatch()
    (tp,) = exits(sim, entry)
    sim.later(30)
    sim.quote(asset, "94.60", "94.65")  # bid below the approved stop 94.70
    summary = sim.monitor_once()
    assert summary.triggers == 1
    assert summary.cancels_requested == 1
    assert sim.ex.order(tp.id).status.value == "CANCEL_PENDING"
    assert len(exits(sim, entry)) == 1  # no protective exit beside the take-profit
    sim.ex.dispatch()  # the take-profit's cancellation is confirmed
    assert sim.ex.order(tp.id).status.value == "CANCELLED"
    summary = sim.monitor_once()
    assert summary.exits_submitted == 1
    stop_exit = exits(sim, entry)[-1]
    assert (stop_exit.order_type, stop_exit.exit_reason, stop_exit.quantity) == (
        OrderType.MARKET,
        ExitReason.STOP_LOSS,
        D(10),
    )
    sim.ex.dispatch()
    sim.later(2)
    sim.quote(asset, "94.55", "94.60")
    sim.later(1)
    sim.simulate()
    (fill,) = sim.ex.fills(stop_exit.id)
    assert fill.price == D("94.52")  # the bid less slippage: not the stop price
    position = sim.position(entry)
    assert (position.status.value, position.exit_reason) == ("CLOSED", ExitReason.STOP_LOSS)
    assert position.realized_pnl == D("-50.60")
    assert position.realized_pnl == ledger_realized(sim, account, asset)
    events = sim.position_events(position.id)
    after = events[events.index("TRIGGERED") :]
    assert after.index("EXIT_SUBMITTED") < after.index("CLOSED")
    assert sim.ex.rv.get(r).consumed_quantity == D(10)  # exits consume nothing
    assert reconciled(sim, account) == "PASSED"


def test_stop_and_target_reached_in_one_bar_assume_the_stop_first(sim):
    account, asset, _r, entry = open_long(sim, "PMB", target="101.00")
    sim.monitor_once()
    sim.ex.dispatch()
    (tp,) = exits(sim, entry)
    start = SESSION + timedelta(minutes=3)
    sim.bar(asset, start, "99.50", "101.50", "94.00", "96.00", "5000")
    sim.at(start + timedelta(minutes=1, seconds=5))
    sim.simulate()  # the broker sees the bar first: it does not fill the take-profit
    assert sim.ex.order(tp.id).filled_quantity == D(0)
    sim.monitor_once()  # stop first: the take-profit is cancelled
    assert sim.position(entry).trigger_reason is ExitReason.STOP_LOSS
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "95.90", "95.95")
    sim.later(1)
    sim.cycle()
    position = sim.position(entry)
    assert (position.status.value, position.exit_reason) == ("CLOSED", ExitReason.STOP_LOSS)
    assert sim.ex.order(tp.id).status.value == "CANCELLED"
    assert reconciled(sim, account) == "PASSED"


# ---------------------------------------------------------------------- trailing stops
def test_a_trailing_stop_ratchets_up_never_down_and_triggers(sim):
    account, asset, _r, entry = open_long(sim, "PMR")
    position = sim.position(entry)
    sim.monitor.tighten(
        position.id, actor="operator:test", reason="trail the winner", trail_amount=D("1.00")
    )
    sim.later(30)
    sim.trade(asset, "100.50", "100")
    sim.later(1)
    sim.monitor_once()
    assert sim.position(entry).trailing_stop_price == D("99.50")
    sim.trade(asset, "102.40", "100")
    sim.later(1)
    sim.monitor_once()
    assert sim.position(entry).trailing_stop_price == D("101.40")
    sim.trade(asset, "101.80", "100")  # lower: the stop does not move down
    sim.later(1)
    sim.monitor_once()
    p = sim.position(entry)
    assert (p.trailing_stop_price, p.trigger_reason) == (D("101.40"), None)
    sim.quote(asset, "101.35", "101.40")
    sim.later(1)
    sim.monitor_once()
    assert sim.position(entry).trigger_reason is ExitReason.TRAILING_STOP
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "101.30", "101.35")
    sim.later(1)
    sim.cycle()
    position = sim.position(entry)
    assert (position.status.value, position.exit_reason) == ("CLOSED", ExitReason.TRAILING_STOP)
    assert position.realized_pnl > 0
    assert "PROTECTION_UPDATED" in sim.position_events(position.id)
    assert reconciled(sim, account) == "PASSED"


def test_protection_can_only_tighten(sim):
    _account, _asset, _r, entry = open_long(sim, "PMX")
    position = sim.position(entry)
    with pytest.raises(Exception, match="loosen"):
        sim.monitor.tighten(position.id, actor="operator:test", reason="no", stop=D("90.00"))
    with pytest.raises(Exception, match="operator"):
        sim.monitor.tighten(position.id, actor="system:execution-x", reason="no", stop=D("96"))
    tightened = sim.monitor.tighten(
        position.id, actor="operator:test", reason="lock in", stop=D("97.00")
    )
    assert tightened.stop_price == D("97.00")
    assert tightened.initial_stop_price == D("94.70")


# ---------------------------------------------------------------------- partial entries
def test_a_stop_withdraws_the_unfilled_entry_remainder_and_exits_what_filled(sim):
    account, asset, r, entry = open_long(sim, "PMP", quantity="25", ask_size="20")
    assert sim.ex.order(entry).filled_quantity == D(10)
    sim.later(30)
    sim.quote(asset, "94.60", "94.65")
    sim.monitor_once()
    assert sim.ex.order(entry).status.value == "CANCEL_PENDING"
    stop_exit = exits(sim, entry)[-1]
    assert stop_exit.quantity == D(10)
    sim.ex.dispatch()
    assert sim.ex.order(entry).status.value == "CANCELLED"
    reservation = sim.ex.rv.get(r)
    assert (reservation.consumed_quantity, reservation.status.value) == (D(10), "RELEASED")
    sim.later(2)
    sim.quote(asset, "94.55", "94.60")
    sim.later(1)
    sim.cycle()
    position = sim.position(entry)
    assert position.status.value == "CLOSED"
    assert ledger_quantity(sim, account, asset) == 0
    assert reconciled(sim, account) == "PASSED"


# ---------------------------------------------------------------------- expiry / close
def test_a_strategy_position_expires_after_its_holding_horizon(sim):
    ex = sim.ex
    account = ex.account("pm-expiry")
    asset = ex.asset("PME")
    r, _proposal = ex.strategy_reservation(account, asset, "pm-res-expiry")
    entry = ex.open_order(account, r, "10", "pm-order-expiry")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    position = sim.position(entry)
    assert position.expires_at == position.opened_at + timedelta(days=5)
    assert position.invalidation_price == D("95.70")
    assert position.strategy_id is not None
    sim.at(position.expires_at + timedelta(minutes=1))
    # Still in a regular session (Monday 2026-11-09 14:32 UTC).
    sim.quote(asset, "99.80", "99.85")
    sim.monitor_once()
    assert sim.position(entry).trigger_reason is ExitReason.EXPIRATION
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "99.81", "99.86")
    sim.later(1)
    sim.cycle()
    closed = sim.position(entry)
    assert (closed.status.value, closed.exit_reason) == ("CLOSED", ExitReason.EXPIRATION)
    assert reconciled(sim, account) == "PASSED"


def test_an_operator_close_request_exits_and_completes(sim):
    account, asset, _r, entry = open_long(sim, "PMC")
    position = sim.position(entry)
    with pytest.raises(Exception, match="operator"):
        sim.monitor.request_close(
            position.id, idempotency_key="close-req-0001", actor="strategy:x", reason="no"
        )
    first = sim.monitor.request_close(
        position.id, idempotency_key="close-req-0001", actor="operator:test", reason="done"
    )
    again = sim.monitor.request_close(
        position.id, idempotency_key="close-req-0001", actor="operator:test", reason="done"
    )
    assert first.created
    assert not again.created
    assert again.request_id == first.request_id
    sim.later(5)
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "99.60", "99.65")
    sim.later(1)
    sim.cycle()
    closed = sim.position(entry)
    assert (closed.status.value, closed.exit_reason) == ("CLOSED", ExitReason.CLOSE_REQUEST)
    assert reconciled(sim, account) == "PASSED"


# ---------------------------------------------------------------------- shorts
def test_a_short_accrues_borrow_daily_covers_on_its_stop_and_settles(sim):
    ex = sim.ex
    account = ex.account("pm-short")
    asset = ex.asset("PMH")
    with session_scope(sim.factory) as session:
        governance.change(
            session,
            account,
            actor="operator:test",
            reason="use the configured minimum short margin (fixture has no provider margin)",
            at=sim.clock.now(),
            hard_limit_updates={"missing_margin_policy": "USE_CONFIGURED_MINIMUM"},
        )
    r = ex.reservation(
        account,
        asset,
        "pm-res-short",
        entry="99.70",
        stop="104.70",
        direction=TradeDirection.SHORT,
    )
    entry = ex.open_order(account, r, "10", "pm-order-short")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.75", "99.80", at=SESSION + timedelta(seconds=50))
    sim.simulate()
    (fill,) = ex.fills(entry)
    assert (fill.side.value, fill.price) == ("SELL_SHORT", D("99.72"))  # bid less slippage
    assert fill.fees > 0  # sell-side regulatory fees
    sim.monitor_once()
    # The next day: one day of borrow on the fresh mark.
    sim.at(SESSION + timedelta(days=1, minutes=2))
    sim.quote(asset, "99.40", "99.45")
    sim.monitor_once()
    p = sim.position(entry)
    assert p.borrow_accrued == D("0.09")  # 10 x 99.425 x 3% / 360, rounded up
    assert "BORROW_ACCRUED" in sim.position_events(p.id)
    sim.later(30)
    sim.quote(asset, "104.70", "104.75")  # ask at the stop
    sim.monitor_once()
    assert sim.position(entry).trigger_reason is ExitReason.STOP_LOSS
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "104.75", "104.80")
    sim.later(1)
    sim.cycle()
    closed = sim.position(entry)
    assert closed.status.value == "CLOSED"
    assert closed.borrow_paid == closed.borrow_accrued == D("0.09")
    assert closed.realized_pnl == ledger_realized(sim, account, asset)
    assert closed.realized_pnl < 0
    assert ledger_quantity(sim, account, asset) == 0
    assert reconciled(sim, account) == "PASSED"


# ---------------------------------------------------------------------- safety
def test_exits_still_work_while_breakers_halt_new_entries(sim):
    account, asset, _r, entry = open_long(sim, "PMK")
    assert sim.ex.risk.service.engage_kill_switch(account, actor="operator:test", reason="drill")
    sim.later(30)
    sim.quote(asset, "94.60", "94.65")
    sim.cycle()
    sim.later(2)
    sim.quote(asset, "94.55", "94.60")
    sim.later(1)
    sim.cycle()
    position = sim.position(entry)
    assert (position.status.value, position.exit_reason) == ("CLOSED", ExitReason.STOP_LOSS)


def test_stale_data_defers_protection_and_exits_nothing(sim):
    _account, asset, _r, entry = open_long(sim, "PMD")
    sim.monitor_once()
    sim.later(600)  # ten minutes without any new observation
    sim.monitor_once()
    position = sim.position(entry)
    assert position.monitor_status == "STALE"
    assert "MONITOR_DEFERRED" in sim.position_events(position.id)
    assert exits(sim, entry) == []
    # A stale price is never used to trigger: an old quote below the stop does not count.
    sim.quote(asset, "94.00", "94.05", at=SESSION + timedelta(minutes=2))
    sim.monitor_once()
    assert sim.position(entry).trigger_reason is None

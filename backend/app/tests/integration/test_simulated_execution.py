"""Phase 7B simulated execution on a real PostgreSQL (TEST FIXTURE market data).

Every quote, trade and bar here is an invented MOCK observation (``FIXTURE``); every fill is
a simulated fill of the isolated paper broker under the default execution model. The tests
check the documented fill rules, the costs, the partial fills and liquidity caps, and that
each fill reaches the 6A ledger (SIMULATED_EXECUTION), the 6C reservation and the position
in one transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.accounting.enums import EntrySource
from app.database.models.accounting import JournalEntryRecord
from app.database.models.reservations import RiskReservationRecord
from app.database.sessions import session_scope
from app.execution.enums import AttemptOutcome, OrderType
from app.tests.integration.simulation_support import SESSION, D, Sim, make_simulation

pytestmark = pytest.mark.integration


@pytest.fixture
def sim(fresh_engine) -> Sim:
    return make_simulation(fresh_engine)


def setup(sim: Sim, symbol: str = "SIMA", *, quantity: str = "10", **order: object):
    ex = sim.ex
    account = ex.account(f"sim-{symbol.lower()}")
    asset = ex.asset(symbol)
    r = ex.reservation(account, asset, f"sim-res-{symbol}")
    order_id = ex.open_order(account, r, quantity, f"sim-order-{symbol}", **order)
    sim.at(SESSION + timedelta(minutes=1))
    return account, asset, r, order_id


def reservation(sim: Sim, rid):
    with session_scope(sim.factory) as session:
        r = session.get(RiskReservationRecord, rid)
        session.expunge(r)
        return r


def sources(sim: Sim, account) -> dict[str, int]:
    with session_scope(sim.factory) as session:
        rows = session.execute(
            select(JournalEntryRecord.source, func.count())
            .where(JournalEntryRecord.portfolio_account_id == account)
            .group_by(JournalEntryRecord.source)
        ).all()
        return {s.value: n for s, n in rows}


# ---------------------------------------------------------------------- market orders
def test_market_buy_fills_at_the_ask_plus_slippage_and_posts_everything(sim):
    account, asset, r, order = setup(sim)
    quote = sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50))
    summary = sim.simulate()
    assert summary.errors == []
    assert summary.fills == 1
    o = sim.ex.order(order)
    assert o.status.value == "FILLED"
    (fill,) = sim.ex.fills(order)
    # 10 of 100 displayed (participation 0.1): 2 bps slippage + 1 bps impact on the ask.
    assert (fill.source.value, fill.price, fill.commission, fill.fees) == (
        "SIMULATION",
        D("99.58"),
        D("1.00"),
        D("0"),
    )
    (detail,) = sim.details(order)
    assert detail.basis.value == "QUOTE"
    assert (detail.touch_price, detail.reference_price) == (D("99.55"), D("99.525"))
    assert detail.participation == D("0.1")
    assert detail.slippage_bps > 0  # adverse to the buyer
    assert detail.observations["quote"]["id"] == quote
    assert sim.book(order).quote_cursor_id == quote
    assert sources(sim, account).get(EntrySource.SIMULATED_EXECUTION.value) == 1
    res = reservation(sim, r)
    assert res.consumed_quantity == D(10)
    assert res.status.value == "RELEASED"  # the unfilled remainder of the approval
    position = sim.position(order)
    assert position is not None
    assert (position.open_quantity, position.average_entry_price) == (D(10), D("99.58"))
    assert position.commissions == D("1.00")
    assert sim.position_events(position.id)[:2] == ["OPENED", "PROTECTION_SET"]
    outcomes = [a.outcome for a in sim.attempts(order)]
    assert outcomes == [AttemptOutcome.FILLED]


def test_the_quote_displayed_at_arrival_is_not_executable(fresh_engine):
    # A model whose equity session is continuous, so the order can be evaluated at NOW.
    from app.paper_broker.execution_model import DEFAULT_MODEL, EQUITY

    model = DEFAULT_MODEL.model_copy(
        update={
            "code": "fixture-continuous",
            "classes": {"EQUITY": EQUITY.model_copy(update={"regular_session_only": False})},
        }
    )
    sim = make_simulation(fresh_engine, model=model)
    ex = sim.ex
    account = ex.account("sim-latency")
    asset = ex.asset("SIML")
    r = ex.reservation(account, asset, "sim-res-latency")
    sim.quote(asset, "99.50", "99.55", at=sim.clock.now() - timedelta(seconds=20))
    order = ex.open_order(account, r, "10", "sim-order-latency")
    sim.later(10)
    sim.simulate()
    assert ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "WAITING_FOR_QUOTE"
    sim.quote(asset, "99.50", "99.56")
    sim.later(1)
    sim.simulate()
    assert ex.order(order).status.value == "FILLED"
    assert sim.ex.fills(order)[0].price == D("99.59")


def test_partial_fills_follow_displayed_liquidity_and_never_reuse_a_quote(sim):
    _account, asset, r, order = setup(sim, "SIMP", quantity="25")
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=40), ask_size="20")
    sim.simulate()
    o = sim.ex.order(order)
    assert (o.status.value, o.filled_quantity) == ("PARTIALLY_FILLED", D(10))
    sim.later(5)
    sim.simulate()  # no new observation: nothing is evaluated, nothing fills again
    assert sim.ex.order(order).filled_quantity == D(10)
    sim.quote(asset, "99.51", "99.56", ask_size="20")
    sim.later(1)
    sim.simulate()
    sim.quote(asset, "99.51", "99.56", ask_size="40")
    sim.later(1)
    sim.simulate()
    o = sim.ex.order(order)
    assert (o.status.value, o.filled_quantity) == ("FILLED", D(25))
    fills = sim.ex.fills(order)
    assert [f.quantity for f in fills] == [D(10), D(10), D(5)]
    # The commission minimum applies to the order, once: 1.00 then nothing more.
    assert [f.commission for f in fills] == [D("1.00"), D("0"), D("0")]
    assert reservation(sim, r).consumed_quantity == D(25)
    assert sim.position(order).open_quantity == D(25)


def test_a_quote_without_size_defers_rather_than_assuming_liquidity(sim):
    _account, asset, _r, order = setup(sim, "SIMN")
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=40), ask_size=None)
    sim.simulate()
    assert sim.ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "LIQUIDITY_UNKNOWN"


def test_stale_quote_defers_and_a_fresh_bar_is_a_labelled_fallback(sim):
    _account, asset, _r, order = setup(sim, "SIMS")
    sim.at(SESSION + timedelta(minutes=10))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(minutes=2))
    sim.simulate()
    assert sim.ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "STALE_QUOTE"
    start = SESSION + timedelta(minutes=9)
    sim.bar(asset, start, "99.40", "99.70", "99.30", "99.60", "1000")
    sim.at(start + timedelta(minutes=1, seconds=5))
    sim.simulate()
    o = sim.ex.order(order)
    assert o.status.value == "FILLED"
    (detail,) = sim.details(order)
    assert detail.basis.value == "BAR_FALLBACK"
    assert detail.reference_price == D("99.60")
    assert D("99.60") < detail.price <= D("99.70")  # widened, inside the bar's range
    assert any("ASSUMED" in a for a in detail.assumptions)


def test_no_fill_while_the_equity_session_is_closed(sim):
    ex = sim.ex
    account = ex.account("sim-closed")
    asset = ex.asset("SIMC")
    r = ex.reservation(account, asset, "sim-res-closed")
    order = ex.open_order(account, r, "10", "sim-order-closed")
    sim.later(60)
    sim.quote(asset, "99.50", "99.55")
    sim.later(1)
    sim.simulate()
    assert ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "MARKET_CLOSED"


def test_an_entry_never_fills_beyond_the_reserved_worst_entry_price(sim):
    _account, asset, _r, order = setup(sim, "SIMW")
    protection = sim.ex.order(order).protection_price
    assert protection is not None
    sim.quote(asset, "102.00", "102.05", at=SESSION + timedelta(seconds=40))
    sim.simulate()
    assert sim.ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "PROTECTION_EXCEEDED"


def test_entry_fills_wait_while_a_breaker_halts_new_entries(sim):
    account, asset, _r, order = setup(sim, "SIMH")
    assert sim.ex.risk.service.engage_kill_switch(account, actor="operator:test", reason="drill")
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=40))
    sim.simulate()
    assert sim.ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "RISK_HALTED"


# ---------------------------------------------------------------------- limit orders
def test_a_limit_buy_is_not_filled_by_a_touch_only_by_trades_through_it(sim):
    _account, asset, _r, order = setup(
        sim, "SIMT", quantity="20", order_type=OrderType.LIMIT, limit_price="99.40"
    )
    sim.quote(asset, "99.45", "99.50", at=SESSION + timedelta(seconds=30))
    sim.trade(asset, "99.40", "500", at=SESSION + timedelta(seconds=35))  # touch only
    sim.simulate()
    assert sim.ex.order(order).status.value == "OPEN"
    assert sim.attempts(order)[-1].code == "LIMIT_NOT_THROUGH"
    sim.trade(asset, "99.39", "100", at=SESSION + timedelta(seconds=70))
    sim.later(15)
    sim.simulate()
    o = sim.ex.order(order)
    # 10% of the 100 traded through, at the limit, never better.
    assert (o.status.value, o.filled_quantity) == ("PARTIALLY_FILLED", D(10))
    (fill,) = sim.ex.fills(order)
    assert fill.price == D("99.40")
    assert sim.details(order)[0].basis.value == "TRADE_THROUGH"


def test_a_marketable_limit_never_fills_above_its_limit(sim):
    _account, asset, _r, order = setup(sim, "SIMM", order_type=OrderType.LIMIT, limit_price="99.56")
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=30))
    sim.simulate()
    (fill,) = sim.ex.fills(order)
    assert fill.price == D("99.56")  # 99.58 after slippage, capped at the limit


def test_bars_fill_a_resting_limit_only_when_traded_strictly_through(sim):
    _account, asset, _r, order = setup(
        sim, "SIMB", quantity="40", order_type=OrderType.LIMIT, limit_price="99.40"
    )
    sim.bar(asset, SESSION + timedelta(minutes=1), "99.60", "99.70", "99.40", "99.50", "2000")
    sim.at(SESSION + timedelta(minutes=2, seconds=5))
    sim.simulate()
    assert sim.ex.order(order).filled_quantity == D(0)  # low == limit: a touch
    sim.bar(asset, SESSION + timedelta(minutes=2), "99.50", "99.55", "99.30", "99.45", "400")
    sim.at(SESSION + timedelta(minutes=3, seconds=5))
    sim.simulate()
    o = sim.ex.order(order)
    assert o.filled_quantity == D(20)  # 5% of 400
    assert sim.ex.fills(order)[0].price == D("99.40")
    assert sim.details(order)[0].basis.value == "BAR_THROUGH"


# ---------------------------------------------------------------------- stops
def test_a_buy_stop_triggers_on_the_ask_and_is_not_filled_at_the_stop(sim):
    _account, asset, _r, order = setup(sim, "SIMG", order_type=OrderType.STOP, stop_price="99.60")
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=30))
    sim.simulate()
    assert sim.attempts(order)[-1].code == "STOP_NOT_TRIGGERED"
    sim.quote(asset, "99.61", "99.63")
    sim.later(1)
    sim.simulate()
    o = sim.ex.order(order)
    assert o.status.value == "FILLED"
    (fill,) = sim.ex.fills(order)
    assert fill.price == D("99.66")  # the ask after slippage: worse than the stop
    assert [e for e in sim.ex.event_types(order) if e == "TRIGGERED"] == ["TRIGGERED"]
    book = sim.book(order)
    assert book.triggered_at is not None
    assert book.trigger_price == D("99.60")


def test_a_triggered_entry_stop_waits_when_the_gap_exceeds_the_reserved_price(sim):
    _account, asset, _r, order = setup(sim, "SIMQ", order_type=OrderType.STOP, stop_price="99.60")
    sim.quote(asset, "99.90", "99.95", at=SESSION + timedelta(seconds=30))  # gapped through
    sim.simulate()
    o = sim.ex.order(order)
    assert o.status.value == "OPEN"
    assert o.filled_quantity == D(0)
    last = sim.attempts(order)[-1]
    assert (last.outcome, last.code) == (AttemptOutcome.TRIGGERED, "PROTECTION_EXCEEDED")


def test_a_triggered_stop_limit_can_remain_unfilled(sim):
    _account, asset, _r, order = setup(
        sim, "SIMK", order_type=OrderType.STOP_LIMIT, stop_price="99.60", limit_price="99.65"
    )
    sim.quote(asset, "99.75", "99.80", at=SESSION + timedelta(seconds=30))
    sim.simulate()
    o = sim.ex.order(order)
    assert o.status.value == "OPEN"
    attempts = sim.attempts(order)
    assert attempts[-1].outcome is AttemptOutcome.TRIGGERED
    assert "TRIGGERED" in sim.ex.event_types(order)
    sim.quote(asset, "99.78", "99.82")
    sim.later(1)
    sim.simulate()
    assert sim.ex.order(order).filled_quantity == D(0)

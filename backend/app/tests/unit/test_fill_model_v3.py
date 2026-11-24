"""Execution model version 3 (D-237): a limit already through on arrival with no fresh quote.

Quotes, trades and bars are TEST FIXTURES. Each test pins what version 2 does with the same
input, so the change is confined to the documented case.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from app.execution.enums import AttemptOutcome, OrderSide, OrderType
from app.paper_broker.execution_model import DEFAULT_MODEL, MODEL_V2
from app.paper_broker.fill_model import ON_ARRIVAL_NOTE, adverse_price, evaluate
from app.tests.unit.test_fill_model import T0, bar, order, quote, trade, view

V3 = DEFAULT_MODEL.for_class("EQUITY")
V2 = MODEL_V2.for_class("EQUITY")
START = T0 - timedelta(minutes=2)


def run(o, v, model):
    return evaluate(o, v, model, market_open=True)


def expected(observed: str, participation: D, *, buys: bool, model=V3) -> D:
    impact = model.impact_bps_at_full_participation * participation
    adjust = (model.assumed_half_spread_bps + model.slippage_bps + impact) / D(10000)
    raw = D(observed) * (1 + adjust) if buys else D(observed) * (1 - adjust)
    return adverse_price(raw, D("0.01"), buys=buys)


def buy_limit(limit: str = "100", *, first_look: bool = True, **kw):
    return order(OrderSide.BUY, OrderType.LIMIT, limit_price=D(limit), first_look=first_look, **kw)


def test_no_quote_first_bar_opened_below_a_buy_limit_fills_at_the_open_plus_costs() -> None:
    v = view(bars=[bar("98", "98.50", "97.50", "98.20", start=START)])
    v2 = run(buy_limit(), v, V2)
    assert v2.fill is not None
    assert v2.fill.price == D("100")  # version 2: at the limit, above the bar's high
    ev = run(buy_limit(), v, V3)
    assert ev.outcome is AttemptOutcome.FILLED
    assert ev.fill is not None
    assert ev.fill.price == expected("98", ev.fill.participation or D(0), buys=True)
    assert D("97.50") <= ev.fill.price <= D("98.50")
    assert ev.fill.assumptions == (ON_ARRIVAL_NOTE,)


def test_a_stale_quote_is_no_quote_and_the_rule_applies() -> None:
    stale = quote("97.90", "98.10", at=T0 - timedelta(minutes=5))  # older than 60 s
    v = view(q=stale, bars=[bar("98", "98.50", "97.50", "98.20", start=START)])
    ev = run(buy_limit(), v, V3)
    assert ev.fill is not None
    assert ev.fill.price == expected("98", ev.fill.participation or D(0), buys=True)


def test_a_fresh_marketable_quote_is_used_as_before() -> None:
    v = view(q=quote("97.90", "98.10"), bars=[bar("98", "98.50", "97.50", "98.20", start=START)])
    a, b = run(buy_limit(), v, V2), run(buy_limit(), v, V3)
    assert a.fill is not None
    assert b.fill is not None
    assert a.fill.price == b.fill.price  # the touch plus slippage: unchanged
    assert ON_ARRIVAL_NOTE not in b.fill.assumptions


def test_a_limit_that_was_resting_when_the_price_crossed_fills_at_the_limit() -> None:
    # The first bar after arrival opened above the limit and traded down through it.
    v = view(bars=[bar("101", "101.50", "99.50", "100.20", start=START)])
    ev = run(buy_limit(), v, V3)
    assert ev.fill is not None
    assert ev.fill.price == D("100")
    # An order that already evaluated data after arrival is resting, whatever opens next.
    later = view(bars=[bar("98", "98.50", "97.50", "98.20", start=START)])
    ev = run(buy_limit(first_look=False), later, V3)
    assert ev.fill is not None
    assert ev.fill.price == D("100")


def test_trades_first_print_through_the_limit_sets_the_price() -> None:
    v = view(trades=[trade("98.40", at=START, tid=1), trade("98.60", at=START, tid=2)])
    ev = run(buy_limit(), v, V3)
    assert ev.fill is not None
    assert ev.fill.price == expected("98.40", ev.fill.participation or D(0), buys=True)
    # A first print at or above the limit: resting; a later print through it fills at it.
    v = view(trades=[trade("100.10", at=START, tid=1), trade("99.80", at=START, tid=2)])
    ev = run(buy_limit(), v, V3)
    assert ev.fill is not None
    assert ev.fill.price == D("100")


def test_a_take_profit_sell_limit_opened_above_fills_at_the_open_less_costs() -> None:
    tp = order(OrderSide.SELL, OrderType.TAKE_PROFIT, limit_price=D("100"), first_look=True)
    v = view(bars=[bar("103", "103.50", "102.50", "103.20", start=START)])
    ev = run(tp, v, V3)
    assert ev.fill is not None
    assert ev.fill.price == expected("103", ev.fill.participation or D(0), buys=False)
    assert ev.fill.price >= D("100")


def test_costs_never_push_the_price_beyond_the_limit() -> None:
    wide = V3.model_copy(update={"assumed_half_spread_bps": D("500")})
    v = view(bars=[bar("99.90", "99.95", "99.50", "99.80", start=START)])
    ev = run(buy_limit(), v, wide)
    assert ev.fill is not None
    assert ev.fill.price == D("99.95")  # inside the bar and never worse than the limit


def test_stops_and_market_orders_are_unchanged_by_version_3() -> None:
    gap_bar = bar("95", "99", "94", "98", start=START)
    stop = order(OrderSide.SELL, OrderType.STOP, stop_price=D("100"), first_look=True)
    a, b = run(stop, view(bars=[gap_bar]), V2), run(stop, view(bars=[gap_bar]), V3)
    assert a.fill is not None
    assert b.fill is not None
    assert a.fill.price == b.fill.price
    market = order(OrderSide.BUY, OrderType.MARKET, first_look=True)
    a, b = run(market, view(bars=[gap_bar]), V2), run(market, view(bars=[gap_bar]), V3)
    assert a.fill is not None
    assert b.fill is not None
    assert a.fill.price == b.fill.price

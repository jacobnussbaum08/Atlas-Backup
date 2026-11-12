"""Phase 7B fill model and protection rules: pure, deterministic checks.

TEST FIXTURE observations only (invented quotes, trades and bars). Every decision here is
a simulated execution decision; nothing is a real fill.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.execution.enums import (
    AttemptOutcome,
    DataBasis,
    ExitReason,
    OrderPurpose,
    OrderSide,
    OrderType,
)
from app.execution.protection import Levels, binding, decide
from app.paper_broker.execution_model import (
    CRYPTO,
    DEFAULT_MODEL,
    EQUITY,
    MODEL_V1,
    ExecutionModel,
)
from app.paper_broker.fill_model import (
    OrderState,
    adverse_price,
    cost_round,
    costs,
    evaluate,
    find_trigger,
    trail_stop,
)
from app.paper_broker.market import Bar, MarketView, Quote, Trade
from app.risk.enums import TradeDirection

T0 = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
FLOOR = T0 - timedelta(minutes=5)


def quote(
    bid: str, ask: str, *, at: datetime = T0, size: str | None = "100", qid: int = 1
) -> Quote:
    return Quote(
        id=qid,
        bid=D(bid),
        ask=D(ask),
        bid_size=None if size is None else D(size),
        ask_size=None if size is None else D(size),
        event_time=at,
        available_at=at,
        provider="alpaca",
        feed="iex",
    )


def trade(price: str, size: str | None = "100", *, at: datetime = T0, tid: int = 1) -> Trade:
    return Trade(tid, D(price), None if size is None else D(size), at)


def bar(
    o: str, h: str, low: str, c: str, v: str | None = "1000", *, start: datetime, bid: int = 1
) -> Bar:
    return Bar(
        bid,
        start,
        start + timedelta(minutes=1),
        D(o),
        D(h),
        D(low),
        D(c),
        None if v is None else D(v),
        start,
    )


def view(
    *, q: Quote | None = None, trades=(), bars=(), as_of: datetime = T0 + timedelta(seconds=5)
) -> MarketView:
    return MarketView(as_of=as_of, floor=FLOOR, quote=q, trades=tuple(trades), bars=tuple(bars))


def order(
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "10",
    **kw,
) -> OrderState:
    purpose = (
        OrderPurpose.ENTRY if side in (OrderSide.BUY, OrderSide.SELL_SHORT) else OrderPurpose.EXIT
    )
    q = D(quantity)
    base = {
        "side": side,
        "order_type": order_type,
        "purpose": purpose,
        "quantity": q,
        "remaining": q,
        "max_fill": q,
        "quantity_increment": D(1),
        "min_quantity": D(1),
        "tick": D("0.01"),
        "minor_units": 2,
    }
    base.update(kw)
    return OrderState(**base)  # type: ignore[arg-type]  # fixture defaults + overrides


def run(o: OrderState, v: MarketView, model=EQUITY, open_=True):
    return evaluate(o, v, model, market_open=open_)


# ---------------------------------------------------------------------- rounding and costs
def test_prices_round_against_the_order_and_costs_round_up():
    assert adverse_price(D("99.5799"), D("0.01"), buys=True) == D("99.58")
    assert adverse_price(D("99.5799"), D("0.01"), buys=False) == D("99.57")
    assert cost_round(D("0.001"), 2) == D("0.01")


def test_commission_minimum_and_cap_apply_to_the_order_not_each_fill():
    o = order(quantity="300")
    first = costs(EQUITY, o, D(100), D("10"))
    assert first.commission == D("1.00")  # 0.50 per-share charge raised to the minimum
    after = replace(o, filled_quantity=D(100), filled_value=D(1000), commission_charged=D("1.00"))
    second = costs(EQUITY, after, D(200), D("10"))
    assert second.commission == D("0.50")  # 300 x 0.005 = 1.50 in all
    tiny = costs(EQUITY, order(quantity="1"), D(1), D("50"))
    assert tiny.commission == D("0.50")  # capped at 1% of a 50.00 notional


def test_sell_side_regulatory_fees_and_none_on_buys():
    buy = costs(EQUITY, order(), D(100), D("100"))
    assert buy.fees == D("0")
    sell = costs(EQUITY, order(OrderSide.SELL), D(100), D("100"))
    # SEC fee 0.0000278 x 10,000 = 0.278 -> 0.28; TAF 0.000166 x 100 = 0.0166 -> 0.02.
    assert (sell.regulatory_fees, sell.exchange_fees) == (D("0.30"), D("0"))
    capped = costs(EQUITY, order(OrderSide.SELL, quantity="100000"), D(100000), D("1"))
    assert capped.regulatory_fees == D("2.78") + D("8.30")
    crypto = costs(CRYPTO, order(), D(2), D("100"))
    assert (crypto.commission, crypto.fees) == (D("0.50"), D("0"))  # 25 bps of 200


# ---------------------------------------------------------------------- market orders
def test_market_buy_executes_at_the_ask_with_slippage_and_impact():
    ev = run(order(), view(q=quote("99.50", "99.55")))
    assert ev.outcome is AttemptOutcome.FILLED
    f = ev.fill
    assert f is not None
    assert (f.quantity, f.price, f.basis) == (D(10), D("99.58"), DataBasis.QUOTE)
    assert f.participation == D("0.1")
    assert f.impact_bps == D("1")
    assert f.slippage_bps is not None
    assert f.slippage_bps > 0


def test_market_sell_executes_at_the_bid():
    ev = run(order(OrderSide.SELL), view(q=quote("99.50", "99.55")))
    assert ev.fill is not None
    assert ev.fill.price == D("99.47")


def test_partial_fill_by_displayed_size_and_lot_size():
    ev = run(
        order(quantity="25", quantity_increment=D(5)), view(q=quote("99.50", "99.55", size="23"))
    )
    assert ev.outcome is AttemptOutcome.PARTIALLY_FILLED
    assert ev.fill is not None
    assert ev.fill.quantity == D(10)


def test_too_little_liquidity_for_one_lot_is_no_fill():
    ev = run(
        order(quantity="200", min_quantity=D(100), quantity_increment=D(100)),
        view(q=quote("1", "1.01", size="150")),
    )
    assert (ev.outcome, ev.code) == (AttemptOutcome.NO_FILL, "INSUFFICIENT_LIQUIDITY")


def test_a_used_quote_is_not_reused():
    ev = run(order(quote_used=7), view(q=quote("99.50", "99.55", qid=7)))
    assert (ev.outcome, ev.code) == (AttemptOutcome.NO_FILL, "WAITING_FOR_QUOTE")


def test_quote_displayed_before_arrival_and_stale_quotes_never_execute():
    early = view(q=quote("99.50", "99.55", at=FLOOR - timedelta(seconds=1)))
    assert run(order(), early).code == "WAITING_FOR_QUOTE"
    stale = view(q=quote("99.50", "99.55", at=T0), as_of=T0 + timedelta(minutes=5))
    assert (run(order(), stale).outcome, run(order(), stale).code) == (
        AttemptOutcome.DEFERRED,
        "STALE_QUOTE",
    )
    assert run(order(), view()).code == "NO_QUOTE"


def test_missing_size_or_closed_market_defers():
    assert run(order(), view(q=quote("99.50", "99.55", size=None))).code == "LIQUIDITY_UNKNOWN"
    assert run(order(), view(q=quote("99.50", "99.55")), open_=False).code == "MARKET_CLOSED"
    unsupported = DEFAULT_MODEL.for_class("FOREX")
    assert (
        run(order(), view(q=quote("1", "1.01")), model=unsupported).code
        == "UNSUPPORTED_ASSET_CLASS"
    )


def test_bar_fallback_is_widened_by_the_assumed_spread_and_stays_in_range():
    b = bar("99.40", "99.70", "99.30", "99.60", "1000", start=T0 - timedelta(minutes=1))
    ev = run(order(), view(bars=[b], as_of=T0 + timedelta(seconds=5)))
    f = ev.fill
    assert f is not None
    assert f.basis is DataBasis.BAR_FALLBACK
    assert D("99.60") < f.price <= D("99.70")
    assert f.quantity == D(10)  # 5% of 1000 = 50 available
    no_volume = bar("99.40", "99.70", "99.30", "99.60", None, start=T0 - timedelta(minutes=1))
    assert run(order(), view(bars=[no_volume])).code == "VOLUME_UNKNOWN"
    off = ExecutionModel(
        classes={"EQUITY": EQUITY.model_copy(update={"allow_bar_fallback": False})}
    )
    assert run(order(), view(bars=[b]), model=off.for_class("EQUITY")).code == "NO_QUOTE"


def test_an_entry_never_fills_beyond_its_reserved_worst_price():
    ev = run(order(protection_price=D("99.57")), view(q=quote("99.50", "99.55")))
    assert (ev.outcome, ev.code) == (AttemptOutcome.NO_FILL, "PROTECTION_EXCEEDED")


# ---------------------------------------------------------------------- limit orders
def test_marketable_limit_never_beyond_the_limit():
    ev = run(
        order(order_type=OrderType.LIMIT, limit_price=D("99.56")), view(q=quote("99.50", "99.55"))
    )
    assert ev.fill is not None
    assert ev.fill.price == D("99.56")
    sell = run(
        order(OrderSide.SELL, OrderType.LIMIT, limit_price=D("99.49")),
        view(q=quote("99.50", "99.55")),
    )
    assert sell.fill is not None
    assert sell.fill.price == D("99.49")


def test_a_touch_is_not_a_fill_only_trades_strictly_through_are():
    o = order(order_type=OrderType.LIMIT, limit_price=D("99.40"), quantity="50")
    touch = run(o, view(q=quote("99.45", "99.50"), trades=[trade("99.40", "1000")]))
    assert (touch.outcome, touch.code) == (AttemptOutcome.NO_FILL, "LIMIT_NOT_THROUGH")
    through = run(o, view(trades=[trade("99.40", "1000"), trade("99.39", "300", tid=2)]))
    assert through.fill is not None
    assert (through.fill.price, through.fill.quantity, through.fill.basis) == (
        D("99.40"),
        D(30),
        DataBasis.TRADE_THROUGH,
    )


def test_bars_fill_a_limit_only_when_strictly_through():
    o = order(order_type=OrderType.LIMIT, limit_price=D("99.40"), quantity="100")
    start = T0 - timedelta(minutes=2)
    assert (
        run(o, view(bars=[bar("99.6", "99.7", "99.40", "99.5", start=start)])).code
        == "LIMIT_NOT_THROUGH"
    )
    ev = run(o, view(bars=[bar("99.6", "99.7", "99.39", "99.5", "600", start=start)]))
    assert ev.fill is not None
    assert (ev.fill.quantity, ev.fill.price) == (D(30), D("99.40"))


def test_a_take_profit_is_not_filled_by_a_bar_that_also_reached_the_stop():
    o = order(
        OrderSide.SELL, OrderType.TAKE_PROFIT, limit_price=D("101.00"), adverse_level=D("94.70")
    )
    start = T0 - timedelta(minutes=2)
    both = bar("99.5", "101.5", "94.0", "96.0", "5000", start=start)
    assert run(o, view(bars=[both])).code == "STOP_REACHED_FIRST"
    trades = [
        trade("101.10", "500", at=T0 - timedelta(seconds=30)),
        trade("94.60", "100", at=T0 - timedelta(seconds=20), tid=2),
    ]
    ev = run(o, view(trades=trades))  # the target traded first: that volume counts
    assert ev.fill is not None
    assert ev.fill.price == D("101.00")


# ---------------------------------------------------------------------- stops
def test_stop_triggers_on_the_touch_and_fills_at_the_market_never_better_than_the_stop():
    o = order(OrderSide.SELL, OrderType.STOP, stop_price=D("95.00"))
    assert run(o, view(q=quote("95.10", "95.15"))).code == "STOP_NOT_TRIGGERED"
    ev = run(o, view(q=quote("94.90", "94.95")))  # a gap through the stop
    assert ev.triggered_now
    assert ev.fill is not None
    assert ev.fill.price == D("94.87")
    assert ev.fill.gap is True
    recovered = run(
        o,
        view(
            trades=[trade("94.99", at=T0 - timedelta(seconds=2))],
            q=quote("95.20", "95.25", at=T0 - timedelta(seconds=10)),
        ),
    )
    assert recovered.fill is not None
    assert recovered.fill.price <= D("95.00")


def test_a_bar_that_opens_beyond_the_stop_is_a_gap():
    hit = find_trigger(
        D("95"),
        buys=False,
        trades=(),
        bars=(bar("94.5", "95.2", "94.0", "94.8", start=T0),),
        quote=None,
    )
    assert hit is not None
    assert hit["gap"] is True
    assert hit["observed"] == "94.5"


def test_an_older_quote_is_superseded_by_later_trades():
    old_quote = quote("94.00", "94.05", at=T0 - timedelta(seconds=30))
    later = (trade("100.00", at=T0 - timedelta(seconds=10)),)
    assert find_trigger(D("95"), buys=False, trades=later, bars=(), quote=old_quote) is None


def test_a_triggered_stop_limit_can_stay_unfilled():
    o = order(order_type=OrderType.STOP_LIMIT, stop_price=D("99.60"), limit_price=D("99.65"))
    ev = run(o, view(q=quote("99.75", "99.80")))
    assert ev.outcome is AttemptOutcome.TRIGGERED
    assert ev.triggered_now
    assert ev.fill is None
    later = replace(o, triggered=True, triggered_at=T0, trigger_level=D("99.60"))
    assert run(later, view(q=quote("99.78", "99.82", qid=2))).outcome is AttemptOutcome.NO_FILL


def test_trailing_stop_ratchets_after_the_observations_it_is_checked_against():
    o = order(
        OrderSide.SELL,
        OrderType.TRAILING_STOP,
        trail_amount=D("1.00"),
        trail_reference=D("100.00"),
        current_stop=D("99.00"),
    )
    # A bar that went to 102 and down to 99.5: checked against 99.00 (not reached), then
    # the reference ratchets to 102 -> stop 101. No intrabar path is assumed.
    start = T0 - timedelta(minutes=2)
    ev = run(o, view(bars=[bar("100", "102", "99.5", "100.5", start=start)]))
    assert ev.code == "STOP_NOT_TRIGGERED"
    assert (ev.trail_reference, ev.current_stop) == (D("102"), D("101.00"))
    lower = replace(o, trail_reference=D("102"), current_stop=D("101.00"))
    down = run(lower, view(trades=[trade("101.50")]))
    assert down.current_stop == D("101.00")  # never down
    hit = run(lower, view(q=quote("100.90", "100.95")))
    assert hit.triggered_now
    assert hit.fill is not None
    assert trail_stop(replace(o, trail_amount=None, trail_percent=D("2")), D("100")) == D("98.00")


# ---------------------------------------------------------------------- protection
def test_binding_level_and_tie_order():
    levels = Levels(
        TradeDirection.LONG,
        stop=D("95"),
        tick=D("0.01"),
        trailing_stop=D("97"),
        invalidation=D("96"),
    )
    assert binding(levels) == (D("97"), ExitReason.TRAILING_STOP)
    tie = Levels(TradeDirection.LONG, stop=D("96"), tick=None, invalidation=D("96"))
    assert binding(tie) == (D("96"), ExitReason.STOP_LOSS)
    short = Levels(TradeDirection.SHORT, stop=D("105"), tick=None, invalidation=D("104"))
    assert binding(short) == (D("104"), ExitReason.INVALIDATION)


def test_protection_stop_first_then_expiry_then_close_request():
    levels = Levels(TradeDirection.LONG, stop=D("95"), tick=D("0.01"))
    both = view(bars=[bar("99", "102", "94", "96", start=T0 - timedelta(minutes=2))])
    assert (
        decide(levels, both, None, now=T0, expires_at=T0, close_requested=True).reason
        is ExitReason.STOP_LOSS
    )
    calm = view(trades=[trade("99")])
    assert (
        decide(levels, calm, None, now=T0, expires_at=T0, close_requested=True).reason
        is ExitReason.EXPIRATION
    )
    assert (
        decide(levels, calm, None, now=T0, expires_at=None, close_requested=True).reason
        is ExitReason.CLOSE_REQUEST
    )
    assert decide(levels, calm, None, now=T0, expires_at=None, close_requested=False).reason is None


def test_position_trailing_stop_only_tightens():
    levels = Levels(
        TradeDirection.LONG,
        stop=D("95"),
        tick=D("0.01"),
        trail_amount=D("2"),
        trail_reference=D("100"),
        trailing_stop=D("98"),
    )
    up = decide(
        levels, view(trades=[trade("103")]), None, now=T0, expires_at=None, close_requested=False
    )
    assert (up.trail_reference, up.trailing_stop) == (D("103"), D("101.00"))
    down = decide(
        replace(levels, trail_reference=D("103"), trailing_stop=D("101")),
        view(trades=[trade("101.5")]),
        None,
        now=T0,
        expires_at=None,
        close_requested=False,
    )
    assert down.trailing_stop == D("101")
    assert down.reason is None


@pytest.mark.parametrize("asset_class", ["EQUITY", "ETF", "CRYPTO"])
def test_the_default_model_is_versioned_and_content_addressed(asset_class):
    assert DEFAULT_MODEL.for_class(asset_class).supported
    same = DEFAULT_MODEL.model_copy()
    assert same.definition_sha256() == DEFAULT_MODEL.definition_sha256()
    changed = ExecutionModel(
        classes={
            **DEFAULT_MODEL.classes,
            asset_class: EQUITY.model_copy(update={"slippage_bps": D("9")}),
        }
    )
    assert changed.definition_sha256() != DEFAULT_MODEL.definition_sha256()


# The content hash of execution model version 1 as Phase 7B registered it. Phase 10B added a
# field (the gap rule) that is omitted while False, so version 1 and the 8A backtest model
# hash exactly as before and their recorded fills stay explained by the same definition.
V1_SHA256 = "00c5ff7e229412d9170baa97e39496f945d0d1935ddea7ede8422e133bd70eff"


def test_version_1_is_unchanged_and_version_2_adds_only_the_gap_rule():
    assert ExecutionModel().definition_sha256() == V1_SHA256
    assert MODEL_V1.definition_sha256() == V1_SHA256
    assert "gap_fill_at_observed" not in MODEL_V1.definition()["classes"]["EQUITY"]
    # A stored version-1 definition loads with the gap rule off.
    assert (
        not ExecutionModel.model_validate(MODEL_V1.definition())
        .for_class("EQUITY")
        .gap_fill_at_observed
    )
    assert DEFAULT_MODEL.version == 2
    v2 = DEFAULT_MODEL.definition()
    for name, rules in v2["classes"].items():
        assert rules.pop("gap_fill_at_observed") is True
        assert rules == MODEL_V1.definition()["classes"][name]
    assert DEFAULT_MODEL.rules[:-1] == MODEL_V1.rules
    assert "gap rule" in DEFAULT_MODEL.rules[-1]


GAP = EQUITY.model_copy(update={"gap_fill_at_observed": True})
LATER = T0 + timedelta(minutes=2)


def _sell_stop(**extra):
    return order(OrderSide.SELL, OrderType.STOP, stop_price=D("100"), **extra)


def test_gap_rule_a_bar_that_opens_below_a_sell_stop_fills_no_better_than_the_open():
    # The bar opened at 95, beyond the stop at 100, and closed at 99. Version 1 fills from the
    # bar's close capped at the stop (about 98.9): better than the market offered after the
    # gap. Version 2 caps at the open less the slippage, within the bar's range.
    b = bar("95", "99.5", "94", "99", "100000", start=T0)
    v1 = run(_sell_stop(), view(bars=[b], as_of=LATER))
    v2 = run(_sell_stop(), view(bars=[b], as_of=LATER), model=GAP)
    assert v1.fill is not None
    assert v2.fill is not None
    assert v1.fill.price > D("95")
    assert v2.fill.price <= D("95") * (1 - GAP.slippage_bps / 10000)
    assert v2.fill.price >= D("94")
    assert v2.trigger is not None
    assert D(v2.trigger["fill_cap"]) == v2.fill.price
    assert any("gap rule" in a for a in v2.fill.assumptions)


def test_gap_rule_a_stop_without_a_gap_is_unchanged():
    b = bar("101", "101.5", "99", "100.5", "100000", start=T0)
    v1 = run(_sell_stop(), view(bars=[b], as_of=LATER))
    v2 = run(_sell_stop(), view(bars=[b], as_of=LATER), model=GAP)
    assert v1.fill is not None
    assert v2.fill is not None
    assert v2.fill.price == v1.fill.price
    assert "fill_cap" not in (v2.trigger or {})


def test_gap_rule_the_recorded_cap_binds_later_evaluations():
    # Triggered earlier through a gap (cap recorded at 94.98); the market has since recovered.
    done = {"triggered": True, "triggered_at": FLOOR, "trigger_level": D("100")}
    q = quote("99.90", "100.00")
    v1 = run(_sell_stop(**done), view(q=q))
    v2 = run(_sell_stop(**done, gap_cap=D("94.98")), view(q=q), model=GAP)
    assert v1.fill is not None
    assert v2.fill is not None
    assert v1.fill.price > D("99")
    assert v2.fill.price == D("94.98")
    # Without a recorded cap (no gap at the trigger), version 2 behaves as version 1.
    v2_plain = run(_sell_stop(**done), view(q=q), model=GAP)
    assert v2_plain.fill is not None
    assert v2_plain.fill.price == v1.fill.price


def test_gap_rule_a_buy_stop_gapping_up_fills_no_better_than_the_open():
    buy = order(OrderSide.BUY, OrderType.STOP, stop_price=D("100"), purpose=OrderPurpose.EXIT)
    b = bar("105", "106", "101", "101.5", "100000", start=T0)
    v1 = run(buy, view(bars=[b], as_of=LATER))
    v2 = run(buy, view(bars=[b], as_of=LATER), model=GAP)
    assert v1.fill is not None
    assert v2.fill is not None
    assert v1.fill.price < D("105")
    assert D("105") <= v2.fill.price <= D("106")

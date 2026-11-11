"""Phase 8A: simulated orders of the backtester, filled by the 7B fill model on daily bars.

Every bar here is a hand-written TEST FIXTURE (prices chosen to hit or miss a level).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtesting.broker import DAILY_EQUITY, SimOrder, backtest_model, evaluate
from app.backtesting.data import DayBar
from app.backtesting.enums import OrderRole
from app.execution.enums import AttemptOutcome, OrderSide, OrderStatus, OrderType
from app.execution.state_machine import InvalidOrderTransitionError

D = Decimal
ASSET = uuid.uuid4()
OPEN = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
MODEL = DAILY_EQUITY


def bar(o: str, h: str, low: str, c: str, volume: str | None = "1000000", day: int = 6) -> DayBar:
    shift = timedelta(days=day - 6)
    return DayBar(
        bar_id=day,
        asset_id=ASSET,
        session=date(2026, 10, day),
        start=OPEN + shift,
        end=CLOSE + shift,
        available_at=CLOSE + shift + timedelta(hours=1),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=None if volume is None else D(volume),
        vwap=None,
        valid=True,
        revision=0,
    )


def order(
    role: OrderRole = OrderRole.ENTRY,
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.LIMIT,
    quantity: str = "100",
    *,
    arrival: datetime = OPEN - timedelta(hours=16),
    **kw: object,
) -> SimOrder:
    o = SimOrder(
        seq=1,
        role=role,
        asset_id=ASSET,
        symbol="FIX",
        asset_class="EQUITY",
        side=side,
        order_type=order_type,
        quantity=D(quantity),
        arrival_at=arrival,
        tick=D("0.01"),
        increment=D(1),
        min_quantity=D(1),
        minor_units=2,
        **kw,  # type: ignore[arg-type]
    )
    o.submit(arrival, "test")
    return o


def test_submission_follows_the_7a_state_machine() -> None:
    o = order(limit_price=D("100"), protection_price=D("100"))
    assert o.status is OrderStatus.OPEN
    assert [e.event.value for e in o.events] == ["CREATED", "APPROVED", "ACCEPTED"]
    with pytest.raises(InvalidOrderTransitionError):
        o.submit(OPEN, "twice")


def test_touching_a_limit_is_not_a_fill_trading_through_it_is() -> None:
    o = order(limit_price=D("100.00"), protection_price=D("100.00"))
    touched = evaluate(o, bar("101", "102", "100.00", "101"), MODEL, max_fill=o.remaining)
    assert touched is not None
    assert touched.outcome is AttemptOutcome.NO_FILL
    assert touched.code == "LIMIT_NOT_THROUGH"
    through = evaluate(o, bar("101", "102", "99.50", "101", day=7), MODEL, max_fill=o.remaining)
    assert through is not None
    assert through.fill is not None
    assert through.fill.price == D("100.00")  # at the limit, never better
    assert o.status is OrderStatus.FILLED


def test_an_order_never_uses_a_bar_that_started_before_it_arrived() -> None:
    o = order(limit_price=D("100"), protection_price=D("100"), arrival=OPEN + timedelta(hours=8))
    assert evaluate(o, bar("101", "102", "90", "95"), MODEL, max_fill=o.remaining) is None
    assert o.filled == 0


def test_partial_fills_are_capped_by_bar_participation() -> None:
    o = order(limit_price=D("100"), protection_price=D("100"), quantity="100")
    # 1,000 shares traded through the limit x 5% participation = 50 shares.
    ev = evaluate(o, bar("101", "102", "99", "101", volume="1000"), MODEL, max_fill=o.remaining)
    assert ev is not None
    assert ev.outcome is AttemptOutcome.PARTIALLY_FILLED
    assert o.filled == D(50)
    assert o.status is OrderStatus.PARTIALLY_FILLED
    ev2 = evaluate(
        o, bar("101", "102", "99", "101", volume="1000", day=7), MODEL, max_fill=o.remaining
    )
    assert ev2 is not None
    assert ev2.outcome is AttemptOutcome.FILLED
    assert o.filled == D(100)


def test_unknown_volume_defers_instead_of_assuming_liquidity() -> None:
    o = order(limit_price=D("100"), protection_price=D("100"))
    ev = evaluate(o, bar("101", "102", "99", "101", volume=None), MODEL, max_fill=o.remaining)
    assert ev is not None
    assert ev.outcome is AttemptOutcome.DEFERRED
    assert o.filled == 0


def test_a_market_entry_never_fills_beyond_the_worst_entry_price() -> None:
    o = order(order_type=OrderType.MARKET, protection_price=D("100.00"))
    ev = evaluate(o, bar("100", "101", "99.8", "100.50"), MODEL, max_fill=o.remaining)
    assert ev is not None
    assert ev.code == "PROTECTION_EXCEEDED"
    assert o.filled == 0
    cheap = evaluate(o, bar("98", "99", "97", "98", day=7), MODEL, max_fill=o.remaining)
    assert cheap is not None
    assert cheap.fill is not None
    # Bar fallback: the close plus the assumed half spread, slippage and impact.
    assert D("98") < cheap.fill.price <= D("100.00")
    assert cheap.fill.basis.value == "BAR_FALLBACK"


def test_a_stop_fills_no_better_than_its_stop_and_gaps_fill_at_the_open() -> None:
    stop = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop_price=D("95"))
    ev = evaluate(stop, bar("97", "98", "94", "97"), MODEL, max_fill=D(100))
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.price == D("95")  # recovered above the stop: still at most the stop
    gap = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop_price=D("95"))
    ev = evaluate(gap, bar("90", "97", "89", "96"), MODEL, max_fill=D(100))
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.gap
    # Opened at 90, below the stop: no better than the open (adjusted for slippage).
    assert ev.fill.price <= D("90")
    assert "gap" in " ".join(ev.fill.assumptions)


def test_a_take_profit_is_not_filled_by_a_bar_that_also_reached_the_stop() -> None:
    tp = order(
        OrderRole.TAKE_PROFIT,
        OrderSide.SELL,
        OrderType.TAKE_PROFIT,
        limit_price=D("110"),
        adverse_level=D("95"),
    )
    ev = evaluate(tp, bar("100", "112", "94", "100"), MODEL, max_fill=D(100))
    assert ev is not None
    assert ev.code == "STOP_REACHED_FIRST"
    assert tp.filled == 0
    ok = evaluate(tp, bar("100", "112", "99", "111", day=7), MODEL, max_fill=D(100))
    assert ok is not None
    assert ok.fill is not None
    assert ok.fill.price == D("110")


def test_exits_never_exceed_what_the_position_holds() -> None:
    stop = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, quantity="100", stop_price=D("95"))
    ev = evaluate(stop, bar("97", "98", "94", "97"), MODEL, max_fill=D(30))
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.quantity == D(30)


def test_costs_follow_the_execution_model() -> None:
    o = order(OrderRole.EXIT, OrderSide.SELL, OrderType.MARKET, quantity="100")
    ev = evaluate(o, bar("100", "101", "99", "100"), MODEL, max_fill=D(100))
    assert ev is not None
    assert ev.fill is not None
    costs = ev.fill.costs
    assert costs.commission == D("1.00")  # 100 x 0.005 = 0.50, minimum 1.00
    assert costs.regulatory_fees > 0  # sell-side fees


def test_the_backtest_model_is_content_addressed_and_overrides_make_a_new_version() -> None:
    a, b = backtest_model(), backtest_model()
    assert a.definition_sha256() == b.definition_sha256()
    custom = backtest_model({"commission_per_unit": "0.01"})
    assert custom.definition_sha256() != a.definition_sha256()
    assert custom.code != a.code
    assert any("gap rule" in r for r in a.rules)

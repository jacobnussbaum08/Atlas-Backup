"""E7 (D-236): the corrected daily-bar execution conventions, and the baseline they replace.

Every bar here is a hand-written TEST FIXTURE. Each test pins the baseline (7B/8A) price
first, so a change to the shared conventions is noticed, then the corrected one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.backtesting.broker import DAILY_EQUITY, SimOrder, evaluate
from app.backtesting.config import ExecutionFidelitySpec, PipelineSpec
from app.backtesting.data import DayBar
from app.backtesting.enums import OrderRole
from app.execution.enums import OrderSide, OrderType
from app.paper_broker.fill_model import adverse_price

D = Decimal
ASSET = uuid.uuid4()
OPEN = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
MODEL = DAILY_EQUITY
ALL_ON = ExecutionFidelitySpec()


def bar(o: str, h: str, low: str, c: str) -> DayBar:
    return DayBar(
        bar_id=6,
        asset_id=ASSET,
        session=date(2026, 10, 6),
        start=OPEN,
        end=CLOSE,
        available_at=CLOSE + timedelta(hours=1),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D("1000000"),
        vwap=None,
        valid=True,
        revision=0,
    )


def order(
    role: OrderRole,
    side: OrderSide,
    kind: OrderType,
    *,
    limit: str | None = None,
    stop: str | None = None,
) -> SimOrder:
    o = SimOrder(
        seq=1,
        role=role,
        asset_id=ASSET,
        symbol="FIX",
        asset_class="EQUITY",
        side=side,
        order_type=kind,
        quantity=D("100"),
        arrival_at=OPEN - timedelta(hours=16),
        tick=D("0.01"),
        increment=D(1),
        min_quantity=D(1),
        minor_units=2,
        limit_price=None if limit is None else D(limit),
        stop_price=None if stop is None else D(stop),
        protection_price=D(limit) if role is OrderRole.ENTRY and limit else None,
    )
    o.submit(o.arrival_at, "test")
    return o


def moved(base: str, half: Decimal, participation: Decimal | None, *, buys: bool) -> Decimal:
    impact = MODEL.impact_bps_at_full_participation * (participation or D(0))
    adjust = (half + MODEL.slippage_bps + impact) / D(10000)
    raw = D(base) * (1 + adjust) if buys else D(base) * (1 - adjust)
    return adverse_price(raw, D("0.01"), buys=buys)


def fill_price(o: SimOrder, b: DayBar, **kw: object) -> tuple[Decimal, Decimal | None]:
    ev = evaluate(o, b, MODEL, max_fill=o.remaining, **kw)  # type: ignore[arg-type]
    assert ev is not None
    assert ev.fill is not None
    return ev.fill.price, ev.fill.participation


def test_a_buy_limit_the_session_opened_below_fills_at_the_open_not_at_an_untraded_limit() -> None:
    # The whole session traded below the limit 101.50.
    b = bar("100", "100.80", "99", "100.50")
    baseline, _ = fill_price(
        order(OrderRole.ENTRY, OrderSide.BUY, OrderType.LIMIT, limit="101.50"), b
    )
    assert baseline == D("101.50")  # the baseline defect: a price above the session's high
    o = order(OrderRole.ENTRY, OrderSide.BUY, OrderType.LIMIT, limit="101.50")
    price, part = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == moved("100", D("4"), part, buys=True)
    assert b.low <= price <= b.high


def test_a_short_entry_limit_the_session_opened_above_fills_at_the_open() -> None:
    b = bar("103", "104", "102.60", "103.50")
    o = order(OrderRole.ENTRY, OrderSide.SELL_SHORT, OrderType.LIMIT, limit="102.00")
    price, part = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == moved("103", D("4"), part, buys=False)
    assert price >= D("102.00")


def test_a_resting_limit_reached_inside_the_session_still_fills_at_the_limit() -> None:
    b = bar("102", "103", "100", "101")  # opened above the buy limit, traded down through it
    o = order(OrderRole.ENTRY, OrderSide.BUY, OrderType.LIMIT, limit="101.50")
    price, _ = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == D("101.50")


def test_a_take_profit_the_session_opened_beyond_fills_at_the_open() -> None:
    b = bar("106", "107", "105.50", "106.50")
    baseline, _ = fill_price(
        order(OrderRole.TAKE_PROFIT, OrderSide.SELL, OrderType.TAKE_PROFIT, limit="105"), b
    )
    assert baseline == D("105")
    o = order(OrderRole.TAKE_PROFIT, OrderSide.SELL, OrderType.TAKE_PROFIT, limit="105")
    price, part = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == moved("106", D("4"), part, buys=False)


def test_a_stop_triggered_inside_the_session_fills_at_its_level_not_at_the_close() -> None:
    b = bar("100", "100.50", "96", "96.50")
    baseline, _ = fill_price(order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98"), b)
    assert baseline < D("96.50")  # the baseline: the close widened by its costs
    o = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98")
    price, part = fill_price(o, b, fidelity=ALL_ON)
    assert price == moved("98", MODEL.assumed_half_spread_bps, part, buys=False)
    assert price < D("98")  # never better than the stop: costs are charged


def test_a_stop_the_session_opened_beyond_fills_at_the_open() -> None:
    b = bar("97", "97.50", "94", "94.50")
    baseline, _ = fill_price(order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98"), b)
    assert baseline < D("95")  # the baseline took the close, worse than the open
    o = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98")
    price, part = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == moved("97", D("4"), part, buys=False)


def test_a_buy_to_cover_stop_mirrors_the_long_rules() -> None:
    b = bar("100", "104", "99.50", "103.50")
    o = order(OrderRole.STOP, OrderSide.BUY_TO_COVER, OrderType.STOP, stop="102")
    price, part = fill_price(o, b, fidelity=ALL_ON)
    assert price == moved("102", MODEL.assumed_half_spread_bps, part, buys=True)


def test_a_market_order_submitted_after_the_close_fills_at_the_next_open() -> None:
    b = bar("100", "101", "98", "98.50")
    baseline, _ = fill_price(order(OrderRole.EXIT, OrderSide.SELL, OrderType.MARKET), b)
    assert baseline < D("98.50")  # the baseline: that session's close
    o = order(OrderRole.EXIT, OrderSide.SELL, OrderType.MARKET)
    price, part = fill_price(o, b, open_half_spread_bps=D("4"), fidelity=ALL_ON)
    assert price == moved("100", D("4"), part, buys=False)


def test_without_a_measured_opening_spread_the_session_half_spread_is_used_and_labelled() -> None:
    b = bar("100", "101", "98", "98.50")
    o = order(OrderRole.EXIT, OrderSide.SELL, OrderType.MARKET)
    ev = evaluate(o, b, MODEL, max_fill=o.remaining, fidelity=ALL_ON)
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.price == moved(
        "100", MODEL.assumed_half_spread_bps, ev.fill.participation, buys=False
    )
    assert any("session/ASSUMED" in a for a in ev.fill.assumptions)


def test_each_correction_can_be_switched_off_alone() -> None:
    b = bar("100", "100.50", "96", "96.50")
    baseline, _ = fill_price(order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98"), b)
    only_limits = ExecutionFidelitySpec(stop_fill_at_trigger=False, market_orders_at_open=False)
    o = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98")
    price, _ = fill_price(o, b, fidelity=only_limits)
    assert price == baseline


def test_corrected_prices_stay_inside_the_session() -> None:
    # The stop is touched by one cent: costs would push the price below the session's low.
    b = bar("100", "100.50", "97.99", "99")
    o = order(OrderRole.STOP, OrderSide.SELL, OrderType.STOP, stop="98")
    price, _ = fill_price(o, b, fidelity=ALL_ON)
    assert price == D("97.99")


def test_the_option_is_absent_by_default() -> None:
    assert PipelineSpec().execution_fidelity is None
    spec = PipelineSpec.model_validate({"execution_fidelity": {"stop_fill_at_trigger": False}})
    assert spec.execution_fidelity is not None
    assert spec.execution_fidelity.limit_marketable_at_open
    assert not spec.execution_fidelity.stop_fill_at_trigger

"""E1 (D-235): opening entries in the backtest broker, and the option's configuration.

Every bar here is a hand-written TEST FIXTURE (prices chosen to open inside or beyond the
worst entry price).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.backtesting.broker import DAILY_EQUITY, SimOrder, evaluate
from app.backtesting.config import OpeningEntrySpec, PipelineSpec
from app.backtesting.data import DayBar
from app.backtesting.enums import OrderRole
from app.execution.enums import OrderSide, OrderType
from app.paper_broker.fill_model import adverse_price

D = Decimal
ASSET = uuid.uuid4()
OPEN = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
MODEL = DAILY_EQUITY


def bar(o: str, h: str, low: str, c: str, day: int = 6) -> DayBar:
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
        volume=D("1000000"),
        vwap=None,
        valid=True,
        revision=0,
    )


def order(side: OrderSide, limit: str, *, opening: bool = True) -> SimOrder:
    o = SimOrder(
        seq=1,
        role=OrderRole.ENTRY,
        asset_id=ASSET,
        symbol="FIX",
        asset_class="EQUITY",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=D("100"),
        arrival_at=OPEN - timedelta(hours=16),
        tick=D("0.01"),
        increment=D(1),
        min_quantity=D(1),
        minor_units=2,
        limit_price=D(limit),
        protection_price=D(limit),
        opening=opening,
    )
    o.submit(o.arrival_at, "test")
    return o


def expected_open_price(
    open_: str, half: Decimal, *, buys: bool, participation: Decimal
) -> Decimal:
    impact = MODEL.impact_bps_at_full_participation * participation
    adjust = (half + MODEL.slippage_bps + impact) / D(10000)
    raw = D(open_) * (1 + adjust) if buys else D(open_) * (1 - adjust)
    return adverse_price(raw, D("0.01"), buys=buys)


def test_a_long_opening_entry_fills_at_the_open_plus_the_measured_half_spread() -> None:
    o = order(OrderSide.BUY, "101.50")
    ev = evaluate(o, bar("100", "103", "99", "102"), MODEL, max_fill=o.remaining,
                  open_half_spread_bps=D("4"))  # fmt: skip
    assert ev is not None
    assert ev.fill is not None
    participation = ev.fill.participation or D(0)
    assert ev.fill.price == expected_open_price(
        "100", D("4"), buys=True, participation=participation
    )
    assert ev.fill.price < D("101.50")  # better than the old fill at the limit
    assert any("measured opening" in a for a in ev.fill.assumptions)


def test_without_a_measured_spread_the_assumed_half_spread_is_used_and_labelled() -> None:
    o = order(OrderSide.BUY, "101.50")
    ev = evaluate(o, bar("100", "103", "99", "102"), MODEL, max_fill=o.remaining)
    assert ev is not None
    assert ev.fill is not None
    participation = ev.fill.participation or D(0)
    assert ev.fill.price == expected_open_price(
        "100", MODEL.assumed_half_spread_bps, buys=True, participation=participation
    )
    assert any("ASSUMED" in a for a in ev.fill.assumptions)


def test_a_short_opening_entry_fills_at_the_open_less_costs() -> None:
    o = order(OrderSide.SELL_SHORT, "98.50")
    ev = evaluate(o, bar("100", "101", "97", "98"), MODEL, max_fill=o.remaining,
                  open_half_spread_bps=D("4"))  # fmt: skip
    assert ev is not None
    assert ev.fill is not None
    participation = ev.fill.participation or D(0)
    assert ev.fill.price == expected_open_price(
        "100", D("4"), buys=False, participation=participation
    )
    assert ev.fill.price > D("98.50")


def test_an_open_beyond_the_worst_price_rests_as_the_limit() -> None:
    o = order(OrderSide.BUY, "101.50")
    ev = evaluate(o, bar("103", "104", "101", "102"), MODEL, max_fill=o.remaining,
                  open_half_spread_bps=D("4"))  # fmt: skip
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.price == D("101.50")  # traded down through the limit: filled at it
    assert not any(a.startswith("opening entry") for a in ev.fill.assumptions)


def test_an_opening_fill_is_never_beyond_the_worst_price() -> None:
    o = order(OrderSide.BUY, "100.02")
    ev = evaluate(o, bar("100", "103", "99", "102"), MODEL, max_fill=o.remaining,
                  open_half_spread_bps=D("50"))  # fmt: skip
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.price == D("100.02")


def test_a_plain_limit_order_is_unchanged() -> None:
    o = order(OrderSide.BUY, "101.50", opening=False)
    ev = evaluate(o, bar("100", "103", "99", "102"), MODEL, max_fill=o.remaining,
                  open_half_spread_bps=D("4"))  # fmt: skip
    assert ev is not None
    assert ev.fill is not None
    assert ev.fill.price == D("101.50")


def test_the_opening_rule_applies_only_to_the_first_session() -> None:
    o = order(OrderSide.BUY, "99.00")
    first = evaluate(o, bar("100", "101", "99.50", "100"), MODEL, max_fill=o.remaining)
    assert first is not None
    assert first.fill is None  # opened beyond the limit and never traded through it
    second = evaluate(o, bar("98", "99.5", "97", "99", day=7), MODEL, max_fill=o.remaining,
                      open_half_spread_bps=D("4"))  # fmt: skip
    assert second is not None
    assert second.fill is not None
    assert second.fill.price == D("99.00")  # the resting-limit rule, not the open


def test_the_option_needs_exactly_one_tolerance_and_is_bounded_by_the_risk_limit() -> None:
    assert OpeningEntrySpec(tolerance_pct=D("0.015")).tolerance_atr is None
    assert OpeningEntrySpec(tolerance_atr=D("0.25")).tolerance_pct is None
    with pytest.raises(ValidationError):
        OpeningEntrySpec()
    with pytest.raises(ValidationError):
        OpeningEntrySpec(tolerance_pct=D("0.01"), tolerance_atr=D("0.25"))
    with pytest.raises(ValidationError):
        OpeningEntrySpec(tolerance_pct=D("0.03"))  # beyond max_entry_deviation_pct (2%)
    assert PipelineSpec().opening_entry is None

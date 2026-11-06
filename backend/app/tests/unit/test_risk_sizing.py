"""Phase 6B position sizing: hand-worked vectors and rounding properties (TEST FIXTURES)."""

from __future__ import annotations

import random
from decimal import Decimal as D
from typing import Any

import pytest

from app.risk.assessment import assess
from app.risk.config import Preferences
from app.risk.enums import AssessmentResult, TradeDirection
from app.risk.sizing import (
    CostAssumptions,
    adverse_levels,
    floor_to_increment,
    max_quantity_for_budget,
    potential_loss_quote,
    to_functional_up,
    to_tick,
    unit_economics,
    verified_quantity,
)
from app.tests.fixtures.risk_inputs import (
    config,
    held,
    inputs,
    instrument,
    market,
    portfolio,
    proposal,
)

LONG, SHORT = TradeDirection.LONG, TradeDirection.SHORT
PASS = AssessmentResult.PASS_FOR_RESERVATION


def costs(**overrides: Any) -> CostAssumptions:
    values: dict[str, Any] = {
        "half_spread": D(0),
        "entry_slippage": D(0),
        "exit_slippage": D(0),
        "fee_rate": D(0),
        "fx_cost_rate": D(0),
        "commission_per_unit": D(0),
        "min_commission": D(0),
        "borrow_rate_annual": D(0),
        "holding_days": 10,
    }
    values.update(overrides)
    return CostAssumptions(**values)


# ------------------------------------------------------------------ the spec formula
def test_spec_formula_without_costs():
    # Budget = 100,000 x 0.5% = 500; raw quantity = 500 / |100 - 95| = 100.
    out = assess(inputs())
    assert out.result is PASS
    assert out.risk_budget == D("500.00")
    assert out.permitted_quantity == D("100")
    assert str(out.permitted_quantity) == "100"
    assert out.potential_loss == D("500.00")  # exactly at the budget is permitted
    assert out.estimated_costs == D("0.00")
    assert out.notional == D("10000.00")
    assert out.binding_constraint == "risk_budget"
    assert D(out.calculations["raw_quantity_initial"]) == D("100")


def test_risk_budget_scales_with_current_equity():
    out = assess(inputs(port=portfolio(equity="80000")))
    assert out.risk_budget == D("400.00")
    assert out.permitted_quantity == D("80")


def test_default_cost_model_vector():
    # entry fill 100 x 1.0005 = 100.05; stop fill 95 x 0.999 = 94.905; price loss 5.145;
    # fees 0.0001 x 194.955 = 0.0194955; variable 5.1644955 per share; commission 0.005/share
    # with a 1.00 minimum per leg. q <= 500 / 5.1744955 = 96.63 and q <= 498 / 5.1644955 =
    # 96.43 -> 96 shares; loss 96 x 5.1644955 + 2 x 1.00 = 497.791568 -> 497.80.
    cfg = config(prefs=Preferences())
    out = assess(inputs(cfg=cfg))
    assert out.permitted_quantity == D("96")
    assert out.potential_loss == D("497.80")
    # Costs beyond the 5.00 price distance: 497.80 - 96 x 5 = 17.80.
    assert out.estimated_costs == D("17.80")


def test_transaction_costs_reduce_the_quantity():
    plain = assess(inputs()).permitted_quantity
    costly = assess(inputs(cfg=config(prefs={"fee_bps": "20"}))).permitted_quantity
    assert plain is not None
    assert costly is not None
    assert costly < plain


def test_minimum_commission_larger_than_budget_is_rejected():
    out = assess(inputs(cfg=config(prefs={"min_commission": "300"})))
    assert out.result is AssessmentResult.REJECTED
    assert "COSTS_EXCEED_BUDGET" in {c.reason_code for c in out.reasons}


# ------------------------------------------------------------------ rounding
def test_ticks_round_levels_adversely():
    assert to_tick(D("100.004"), D("0.01"), up=True) == D("100.01")
    assert to_tick(D("95.006"), D("0.01"), up=False) == D("95.00")
    assert to_tick(D("100.00"), D("0.01"), up=True) == D("100.00")
    assert adverse_levels(LONG, D("100.004"), D("95.006"), D("0.01")) == (
        D("100.01"),
        D("95.00"),
    )
    assert adverse_levels(SHORT, D("50.006"), D("51.994"), D("0.01")) == (
        D("50.00"),
        D("52.00"),
    )


def test_tick_rounding_can_only_reduce_the_quantity():
    # Unrounded distance 4.998 would allow 100 shares; on the tick grid it is 5.01 -> 99.
    out = assess(inputs(prop=proposal(entry="100.004", stop="95.006"), mkt=market("100")))
    assert out.calculations["entry_adjusted"] == "100.01"
    assert out.calculations["stop_adjusted"] == "95.00"
    assert out.permitted_quantity == D("99")
    assert out.potential_loss is not None
    assert out.potential_loss <= D("500.00")


def test_sub_dollar_prices_use_the_sub_penny_tick():
    out = assess(inputs(prop=proposal(entry="0.5000", stop="0.4500"), mkt=market("0.5")))
    assert out.calculations["tick_size"] == "0.0001"


def test_floor_to_increment_never_rounds_up():
    assert floor_to_increment(D("166.6666"), D("0.001")) == D("166.666")
    assert floor_to_increment(D("99.999"), D("1")) == D("99")
    assert floor_to_increment(D("0.0009"), D("0.001")) == D("0")
    assert floor_to_increment(D("10"), D("5")) == D("10")
    assert floor_to_increment(D("-1"), D("1")) == D("0")


def test_fractional_lot_vector():
    # 500 / 3 = 166.666... -> 166.666; loss 499.998 rounds up to 500.00, still <= 500.
    out = assess(
        inputs(
            prop=proposal(entry="100", stop="97"),
            ins=instrument(quantity_increment=D("0.001"), min_quantity=D("0.001")),
        )
    )
    assert out.permitted_quantity == D("166.666")
    assert out.potential_loss == D("500.00")


def test_currency_double_rounding_steps_down_a_lot():
    # EUR instrument, 1.1 USD per EUR, distance 3: raw 500 / 3.3 = 151.5151 -> 151.515 EUR
    # loss 454.545 -> 454.55 EUR -> 500.005 USD -> 500.01 > 500: two steps down to 151.513
    # (454.539 -> 454.54 EUR -> 499.994 -> 500.00 USD).
    out = assess(
        inputs(
            prop=proposal(entry="100", stop="97"),
            ins=instrument(
                quote_currency="EUR", quantity_increment=D("0.001"), min_quantity=D("0.001")
            ),
            mkt=market("100", fx_rate=D("1.1")),
        )
    )
    assert out.result is PASS
    assert out.permitted_quantity == D("151.513")
    assert out.potential_loss == D("500.00")


def test_functional_conversion_rounds_up_twice():
    assert to_functional_up(D("454.545"), D("1.1"), "EUR", "USD") == D("500.01")
    assert to_functional_up(D("100"), D("150"), "USD", "USD") == D("15000.00")
    # JPY has no minor units: 1000.4 JPY rounds up to 1001 before conversion.
    assert to_functional_up(D("1000.4"), D("0.0067"), "JPY", "USD") == D("6.71")


@pytest.mark.parametrize("seed", range(12))
def test_rounding_never_exceeds_the_budget_and_is_maximal(seed):
    """Seeded random vectors: the verified quantity never risks more than the budget, and one
    more lot would (so rounding never gives risk away either)."""
    rng = random.Random(seed)  # noqa: S311 - deterministic test vectors, not security
    for _ in range(120):
        entry = D(rng.randint(100, 50_000)) / 100
        distance = D(rng.randint(1, 1_000)) / 100
        direction = rng.choice([LONG, SHORT])
        stop = entry - distance if direction is LONG else entry + distance
        if stop <= 0:
            continue
        increment = rng.choice([D(1), D("0.1"), D("0.001"), D("0.000001")])
        c = costs(
            half_spread=D(rng.randint(0, 30)) / 10_000,
            entry_slippage=D(rng.randint(0, 20)) / 10_000,
            exit_slippage=D(rng.randint(0, 40)) / 10_000,
            fee_rate=D(rng.randint(0, 5)) / 10_000,
            commission_per_unit=D(rng.randint(0, 10)) / 1000,
            min_commission=D(rng.randint(0, 200)) / 100,
            borrow_rate_annual=D(rng.randint(0, 10)) / 100 if direction is SHORT else D(0),
            holding_days=rng.randint(1, 30),
        )
        rate = rng.choice([D(1), D("1.0835"), D("0.0067"), D("1.27")])
        quote = {D(1): "USD", D("1.0835"): "EUR", D("0.0067"): "JPY", D("1.27"): "GBP"}[rate]
        budget = D(rng.randint(1_000, 200_000)) / 100
        unit = unit_economics(direction, entry, stop, c)
        raw = max_quantity_for_budget(budget, unit, c, rate)
        quantity, loss = verified_quantity(raw, increment, budget, unit, c, rate, quote, "USD")
        if quantity == 0:
            continue
        assert loss <= budget
        assert loss == to_functional_up(potential_loss_quote(quantity, unit, c), rate, quote, "USD")
        bigger = to_functional_up(
            potential_loss_quote(quantity + increment, unit, c), rate, quote, "USD"
        )
        assert bigger > budget


# ------------------------------------------------------------------ long and short
def test_short_gets_half_the_risk_budget():
    # 100,000 x 0.5% x 0.5 = 250; |50 - 52| = 2 -> 125 shares.
    out = assess(inputs(prop=proposal(SHORT, entry="50", stop="52"), mkt=market("50")))
    assert out.result is PASS
    assert out.risk_budget == D("250.00")
    assert out.permitted_quantity == D("125")
    assert out.potential_loss == D("250.00")


def test_short_includes_borrow_cost():
    # Borrow 5% a year for 10 days on 50: 0.0684931... per share; 250 / 2.0684931 = 120.86.
    out = assess(
        inputs(
            prop=proposal(SHORT, entry="50", stop="52"),
            mkt=market("50"),
            cfg=config(prefs={"short_borrow_rate_annual": "0.05"}),
        )
    )
    assert out.permitted_quantity == D("120")
    assert out.potential_loss == D("248.22")
    assert "BORROW_COST_ASSUMED" in {c.reason_code for c in out.checks}


def test_short_stop_slippage_works_against_the_short():
    unit = unit_economics(SHORT, D("50"), D("52"), costs(exit_slippage=D("0.001")))
    assert unit.exit_fill == D("52.052")
    assert unit.price_loss == D("2.052")


def test_long_loss_cannot_exceed_the_entry_price():
    unit = unit_economics(LONG, D("1"), D("0.0001"), costs(exit_slippage=D("0.5")))
    assert unit.exit_fill == D("0.00005")
    unit = unit_economics(LONG, D("1"), D("0.0001"), costs(exit_slippage=D("2")))
    assert unit.exit_fill == 0
    assert unit.price_loss == D("1")


def test_existing_same_direction_position_can_be_added_to():
    existing = held("FIXT", "5000", asset_id=instrument().asset_id)
    out = assess(inputs(port=portfolio(positions=[existing])))
    assert out.result is PASS
    # Single-asset room 20,000 - 5,000 = 15,000 -> 150 shares; the risk budget allows 100.
    assert out.permitted_quantity == D("100")

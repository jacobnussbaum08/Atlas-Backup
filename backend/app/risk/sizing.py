"""Deterministic position-sizing arithmetic (spec 14.2; decision D-142). Pure functions.

Initial calculation (spec 14.2)::

    risk budget  = current equity x permitted risk percentage
    raw quantity = risk budget / |entry - stop|

Atlas then makes every adjustment in the conservative direction:

* the entry is moved **adversely** to the instrument's tick (up for a long, down for a short)
  and the stop is moved **away** from the entry (down for a long, up for a short), so tick
  rounding can only widen the risk per unit;
* the loss per unit includes the half spread and entry slippage, the half spread and stop
  slippage at the exit (a stop is not a guaranteed price), fees on both legs, a currency
  conversion cost for a foreign instrument, and borrow cost over the holding period for a
  short; commissions are per unit with a per-leg minimum;
* the quantity is solved so the total potential loss fits the budget, floored to the lot
  increment, and then verified with every amount rounded **up** (losses and costs to the
  currency's minor units, then to the functional currency's) against the budget rounded
  **down**: rounding can never increase risk beyond the permitted budget. If the verification
  fails the quantity steps down one lot at a time.

All values are Decimals in the 60-digit trapping context of ``app.accounting.money``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from app.accounting.money import ONE, ZERO, currency, div, money_context, quantum
from app.risk.enums import TradeDirection

BPS = Decimal("0.0001")
DAYS_PER_YEAR = Decimal(365)
MAX_STEP_DOWN = 10_000


def ceil_to(value: Decimal, places: int) -> Decimal:
    with money_context():
        return value.quantize(quantum(places), rounding=ROUND_CEILING)


def floor_to(value: Decimal, places: int) -> Decimal:
    with money_context():
        return value.quantize(quantum(places), rounding=ROUND_FLOOR)


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """The largest multiple of ``increment`` that is <= ``value`` (never rounds up)."""
    if increment <= ZERO:
        raise ValueError("increment must be positive")
    if value <= ZERO:
        return ZERO
    with money_context():
        steps = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
        # Carry only the increment's significant places (a NUMERIC(38,18) lot of
        # 1.000000000000000000 gives whole units).
        exponent = increment.normalize().as_tuple().exponent
        places = min(exponent, 0) if isinstance(exponent, int) else 0
        return (steps * increment).quantize(Decimal(1).scaleb(places))


def to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    """``price`` moved onto the tick grid, up or down (unchanged when already on it)."""
    if tick <= ZERO:
        raise ValueError("tick size must be positive")
    with money_context():
        steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        return steps * tick


def adverse_levels(
    direction: TradeDirection, entry: Decimal, stop: Decimal, tick: Decimal
) -> tuple[Decimal, Decimal]:
    """(entry, stop) on the tick grid, each moved so the distance can only grow."""
    if direction is TradeDirection.LONG:
        return to_tick(entry, tick, up=True), to_tick(stop, tick, up=False)
    return to_tick(entry, tick, up=False), to_tick(stop, tick, up=True)


@dataclass(frozen=True)
class CostAssumptions:
    """Per-assessment cost model in the quote currency (all fractions, not bps)."""

    half_spread: Decimal
    entry_slippage: Decimal
    exit_slippage: Decimal
    fee_rate: Decimal
    fx_cost_rate: Decimal
    commission_per_unit: Decimal
    min_commission: Decimal
    borrow_rate_annual: Decimal
    holding_days: int


@dataclass(frozen=True)
class UnitEconomics:
    """Per-unit prices and loss for one direction and set of levels (quote currency)."""

    entry_fill: Decimal
    exit_fill: Decimal
    price_loss: Decimal
    fees: Decimal
    fx_cost: Decimal
    borrow_cost: Decimal
    # Loss per unit excluding commissions (multiplier applied).
    variable_loss: Decimal
    # Price used for exposure limits: the entry fill against the trader (both directions).
    exposure_price: Decimal
    # Entry-leg costs per unit (fees and conversion on the entry fill), for capital checks.
    entry_costs: Decimal


def unit_economics(
    direction: TradeDirection,
    entry: Decimal,
    stop: Decimal,
    costs: CostAssumptions,
    multiplier: Decimal = ONE,
) -> UnitEconomics:
    with money_context():
        adverse_in = costs.half_spread + costs.entry_slippage
        adverse_out = costs.half_spread + costs.exit_slippage
        if direction is TradeDirection.LONG:
            entry_fill = entry * (ONE + adverse_in)
            # A long can lose at most what it paid: the exit price cannot go below zero.
            exit_fill = max(stop * (ONE - adverse_out), ZERO)
            price_loss = entry_fill - exit_fill
            borrow = ZERO
        else:
            entry_fill = entry * (ONE - adverse_in)
            exit_fill = stop * (ONE + adverse_out)
            price_loss = exit_fill - entry_fill
            borrow = (
                costs.borrow_rate_annual * entry_fill * Decimal(costs.holding_days) / DAYS_PER_YEAR
            )
        fees = costs.fee_rate * (entry_fill + exit_fill)
        fx = costs.fx_cost_rate * (entry_fill + exit_fill)
        variable = (price_loss + fees + fx + borrow) * multiplier
        exposure_price = entry * (ONE + adverse_in)
        entry_costs = (costs.fee_rate + costs.fx_cost_rate) * entry_fill * multiplier
    return UnitEconomics(
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        price_loss=price_loss,
        fees=fees,
        fx_cost=fx,
        borrow_cost=borrow,
        variable_loss=variable,
        exposure_price=exposure_price,
        entry_costs=entry_costs,
    )


def commission(quantity: Decimal, costs: CostAssumptions) -> Decimal:
    """Commission of one leg (quote currency, unrounded)."""
    if quantity <= ZERO:
        return ZERO
    with money_context():
        return max(costs.min_commission, quantity * costs.commission_per_unit)


def potential_loss_quote(quantity: Decimal, unit: UnitEconomics, costs: CostAssumptions) -> Decimal:
    """Total loss if the stop is hit, both commissions included (quote currency, unrounded)."""
    if quantity <= ZERO:
        return ZERO
    with money_context():
        return quantity * unit.variable_loss + 2 * commission(quantity, costs)


def to_functional_up(amount_quote: Decimal, rate: Decimal, quote: str, functional: str) -> Decimal:
    """A loss or cost in the functional currency, rounded up twice (never understated)."""
    in_quote = ceil_to(amount_quote, currency(quote).minor_units)
    with money_context():
        converted = in_quote * rate
    return ceil_to(converted, currency(functional).minor_units)


def max_quantity_for_budget(
    budget: Decimal, unit: UnitEconomics, costs: CostAssumptions, rate: Decimal
) -> Decimal:
    """Unrounded largest quantity whose potential loss (functional) fits ``budget``.

    loss(q) = q x variable + 2 x max(min_commission, q x commission_per_unit), so
    loss(q) <= B  <=>  q <= B / (variable + 2c)  and  q <= (B - 2 min) / variable.
    """
    if budget <= ZERO or unit.variable_loss <= ZERO:
        return ZERO
    with money_context():
        per_unit = (unit.variable_loss + 2 * costs.commission_per_unit) * rate
        by_rate = budget / per_unit
        after_minimum = budget - 2 * costs.min_commission * rate
        if after_minimum <= ZERO:
            return ZERO
        by_minimum = after_minimum / (unit.variable_loss * rate)
        return min(by_rate, by_minimum)


def verified_quantity(
    raw: Decimal,
    increment: Decimal,
    budget: Decimal,
    unit: UnitEconomics,
    costs: CostAssumptions,
    rate: Decimal,
    quote: str,
    functional: str,
) -> tuple[Decimal, Decimal]:
    """(quantity, rounded potential loss): floored to the lot and verified against the budget.

    The loss is recomputed with every rounding against Atlas; while it exceeds the budget the
    quantity steps down one lot. Returns (0, 0) when not even one lot fits.
    """
    quantity = floor_to_increment(raw, increment)
    for _ in range(MAX_STEP_DOWN):
        if quantity <= ZERO:
            return ZERO, ZERO
        loss = to_functional_up(
            potential_loss_quote(quantity, unit, costs), rate, quote, functional
        )
        if loss <= budget:
            return quantity, loss
        quantity -= increment
    return ZERO, ZERO


def capacity_quantity(capacity: Decimal, per_unit: Decimal, fixed: Decimal = ZERO) -> Decimal:
    """Unrounded quantity that fits ``capacity`` at ``per_unit`` plus ``fixed`` (>= 0)."""
    if per_unit <= ZERO:
        raise ValueError("per-unit requirement must be positive")
    remaining = capacity - fixed
    if remaining <= ZERO:
        return ZERO
    return div(remaining, per_unit)


# ------------------------------------------------------------------ instrument sizers
class InstrumentSizer(Protocol):
    """Instrument-specific sizing rules (spec 14.2: stocks, forex and crypto differ)."""

    @property
    def name(self) -> str: ...

    @property
    def supports_short(self) -> bool: ...

    @property
    def supported(self) -> bool: ...

    @property
    def unsupported_reason(self) -> str: ...

    def tick_size(self, provider_tick: Decimal | None, price: Decimal) -> Decimal | None:
        """The price increment to round levels to, or None when it is unknown."""
        ...


@dataclass(frozen=True)
class ShareSizer:
    """US-listed stocks and ETFs: whole or fractional shares by the registered lot size.

    Tick size: the provider's reported increment if any, otherwise the SEC Rule 612 minimum
    pricing increment (USD 0.01 at or above USD 1.00, USD 0.0001 below), a regulatory rule,
    not an estimate.
    """

    name: str = "SHARES"
    supports_short: bool = True
    supported: bool = True
    unsupported_reason: str = ""

    def tick_size(self, provider_tick: Decimal | None, price: Decimal) -> Decimal | None:
        if provider_tick is not None and provider_tick > ZERO:
            return provider_tick
        return Decimal("0.01") if price >= ONE else Decimal("0.0001")


@dataclass(frozen=True)
class CryptoSizer:
    """Crypto: fractional units by the registered lot size, long only.

    The tick size must be reported by the provider (no regulatory default exists). Shorting
    crypto needs a borrow and margin model Atlas does not have, so shorts are refused.
    """

    name: str = "CRYPTO_UNITS"
    supports_short: bool = False
    supported: bool = True
    unsupported_reason: str = ""

    def tick_size(self, provider_tick: Decimal | None, price: Decimal) -> Decimal | None:
        if provider_tick is not None and provider_tick > ZERO:
            return provider_tick
        return None


@dataclass(frozen=True)
class ForexSizer:
    """Forex: an extension point. The ledger holds currencies as balances, not positions
    (Phase 6A), and no contract specification (pip value, contract size) exists, so forex
    proposals are refused rather than sized with guessed contract terms."""

    name: str = "FOREX_CONTRACTS"
    supports_short: bool = False
    supported: bool = False
    unsupported_reason: str = (
        "forex sizing needs contract specifications (contract size, pip value) and forex "
        "positions in the ledger; neither exists yet"
    )

    def tick_size(self, provider_tick: Decimal | None, price: Decimal) -> Decimal | None:
        return None


SIZERS: dict[str, InstrumentSizer] = {
    "EQUITY": ShareSizer(),
    "ETF": ShareSizer(),
    "CRYPTO": CryptoSizer(),
    "FOREX": ForexSizer(),
}


def sizer_for(asset_class: str) -> InstrumentSizer | None:
    return SIZERS.get(asset_class)


__all__ = [
    "BPS",
    "SIZERS",
    "CostAssumptions",
    "CryptoSizer",
    "ForexSizer",
    "InstrumentSizer",
    "ShareSizer",
    "UnitEconomics",
    "adverse_levels",
    "capacity_quantity",
    "ceil_to",
    "commission",
    "floor_to",
    "floor_to_increment",
    "max_quantity_for_budget",
    "potential_loss_quote",
    "sizer_for",
    "to_functional_up",
    "to_tick",
    "unit_economics",
    "verified_quantity",
]

"""Decimal arithmetic and rounding rules for the ledger (spec 3.5, 16.5; decision D-126).

* Every financial value is a :class:`~decimal.Decimal`; floats are refused outright.
* Arithmetic runs in a 60-digit context that traps invalid operations, division by zero
  and overflow, so nothing is silently lost before an explicit rounding step.
* Explicit rounding is always ``ROUND_HALF_EVEN`` and happens only at the points documented
  in ``docs/architecture/accounting.md``:

  - a currency amount computed from quantity x price, or from a proportional relief, is
    rounded to the currency's minor units;
  - a functional (reporting-currency) value is rounded to the functional currency's minor
    units (cents for USD): the ledger is kept in the reporting currency's own precision;
  - an inverted FX rate is rounded to ``RATE_SCALE`` places.

* Inputs are never rounded: an amount with more decimals than its currency allows, or a
  quantity that is not a multiple of the instrument's increment, is rejected.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

RATE_SCALE = 12
PRICE_SCALE = 10
QUANTITY_SCALE = 18
ROUNDING = ROUND_HALF_EVEN

ZERO = Decimal(0)
ONE = Decimal(1)

_CONTEXT = Context(prec=60, rounding=ROUNDING, traps=[InvalidOperation, DivisionByZero, Overflow])


class MoneyError(ValueError):
    """An amount, rate or quantity violates the precision rules."""


@contextmanager
def money_context() -> Iterator[Context]:
    with localcontext(_CONTEXT) as ctx:
        yield ctx


def to_decimal(value: object, what: str = "value") -> Decimal:
    """A finite Decimal from a Decimal, int or numeric string. Floats are refused."""
    if isinstance(value, bool | float):
        raise MoneyError(f"{what} must be a Decimal or a string, never a float or bool")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise MoneyError(f"{what} is not a number") from None
    else:
        raise MoneyError(f"{what} must be a Decimal, int or numeric string")
    if not result.is_finite():
        raise MoneyError(f"{what} must be finite")
    return result


def scale_of(value: Decimal) -> int:
    """Number of decimal places carried by ``value`` (0 for integers)."""
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        raise MoneyError("not a finite number")
    return max(0, -exponent)


def quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def round_to(value: Decimal, places: int) -> Decimal:
    with money_context():
        return value.quantize(quantum(places), rounding=ROUNDING)


def functional_places(functional_currency: str) -> int:
    return currency(functional_currency).minor_units


def round_functional(value: Decimal, functional_currency: str) -> Decimal:
    return round_to(value, functional_places(functional_currency))


def require_scale(value: Decimal, places: int, what: str) -> Decimal:
    """Reject (never round) an input that carries more than ``places`` decimals."""
    if scale_of(value) > places:
        raise MoneyError(f"{what} {value} has more than {places} decimal places")
    return round_to(value, places)


def require_positive(value: Decimal, what: str) -> Decimal:
    if value <= ZERO:
        raise MoneyError(f"{what} must be positive")
    return value


def require_non_negative(value: Decimal, what: str) -> Decimal:
    if value < ZERO:
        raise MoneyError(f"{what} must not be negative")
    return value


def mul(*factors: Decimal) -> Decimal:
    with money_context():
        result = ONE
        for factor in factors:
            result *= factor
        return result


def div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        raise MoneyError("division by zero")
    with money_context():
        return numerator / denominator


def proportion(total: Decimal, part: Decimal, whole: Decimal, places: int) -> Decimal:
    """``total * part / whole`` rounded to ``places``; exactly ``total`` when part == whole.

    Used for average-cost relief: relieving the whole balance always relieves all of it, so
    a closed position never leaves a rounding residue behind.
    """
    if whole == ZERO:
        raise MoneyError("proportion of an empty balance")
    if part == whole:
        return total
    with money_context():
        return round_to(total * part / whole, places)


@dataclass(frozen=True)
class CurrencySpec:
    code: str
    name: str
    minor_units: int

    def amount(self, value: Decimal, what: str = "amount") -> Decimal:
        return require_scale(value, self.minor_units, f"{what} in {self.code}")

    def round(self, value: Decimal) -> Decimal:
        return round_to(value, self.minor_units)


# Fiat currencies the ledger accepts. Mirrored by the ``currencies`` reference table
# (migration 0013); a test keeps the two identical. Crypto assets are instruments with a
# quantity (asset_quantity_ledger), not cash currencies.
CURRENCIES: dict[str, CurrencySpec] = {
    spec.code: spec
    for spec in (
        CurrencySpec("USD", "US dollar", 2),
        CurrencySpec("EUR", "Euro", 2),
        CurrencySpec("GBP", "Pound sterling", 2),
        CurrencySpec("JPY", "Japanese yen", 0),
        CurrencySpec("CHF", "Swiss franc", 2),
        CurrencySpec("CAD", "Canadian dollar", 2),
        CurrencySpec("AUD", "Australian dollar", 2),
    )
}


def currency(code: str) -> CurrencySpec:
    try:
        return CURRENCIES[code]
    except KeyError:
        raise MoneyError(f"unsupported currency {code!r}") from None


__all__ = [
    "CURRENCIES",
    "ONE",
    "PRICE_SCALE",
    "QUANTITY_SCALE",
    "RATE_SCALE",
    "ROUNDING",
    "ZERO",
    "CurrencySpec",
    "MoneyError",
    "currency",
    "div",
    "functional_places",
    "money_context",
    "mul",
    "proportion",
    "quantum",
    "require_non_negative",
    "require_positive",
    "require_scale",
    "round_functional",
    "round_to",
    "scale_of",
    "to_decimal",
]

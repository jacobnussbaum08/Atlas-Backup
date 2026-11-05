"""Journal entry drafts and the balancing rules every posting must satisfy (D-128).

A draft is what a posting template produces before anything is written. :func:`validate`
enforces, in Python, the same rules the database re-checks at commit
(``atlas_journal_entry_balanced``, migration 0013):

R1  For every currency in the entry, the sum of debit amounts equals the sum of credit
    amounts. No currency is ever created, destroyed or mixed with another one; a
    conversion goes through the FX clearing account so each currency balances on its own.
R2  The sum of functional (reporting-currency) debits equals the sum of functional credits.

Line rules:

* an ordinary line has a positive amount in its currency, quantized to that currency's
  minor units, and a non-negative functional value (8 places);
* a functional-only line (FX translation, FX differences) has a zero amount, a positive
  functional value, a currency other than the functional currency, and is allowed only on
  accounts that accept it;
* a line in the functional currency has functional value == amount and rate 1;
* a line on an account that requires an asset names one; accounts that forbid one never do.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.accounting.chart import CHART, AssetRule
from app.accounting.enums import EntryType, MovementType, Side
from app.accounting.money import (
    ONE,
    RATE_SCALE,
    ZERO,
    MoneyError,
    currency,
    scale_of,
)


class RateBasis(StrEnum):
    """How a line's functional value was obtained."""

    # The line is in the functional currency: functional value == amount.
    FUNCTIONAL = "FUNCTIONAL"
    # amount x a timestamped rate from ``fx_rates`` (fx_rate_id set).
    SPOT = "SPOT"
    # The historical carrying rate of the balance being relieved (average cost).
    CARRYING = "CARRYING"
    # Implied by the two legs of a currency conversion (reporting-currency leg / amount).
    TRANSACTION = "TRANSACTION"
    # A functional-only FX difference line: no currency amount, no rate.
    NONE = "NONE"


class EntryValidationError(ValueError):
    """A draft violates a balancing or line rule. Nothing is posted."""


@dataclass(frozen=True)
class DraftLine:
    account_code: str
    side: Side
    currency: str
    amount: Decimal
    functional_amount: Decimal
    rate_basis: RateBasis
    fx_rate: Decimal | None
    fx_rate_id: int | None = None
    asset_id: uuid.UUID | None = None
    memo: str | None = None

    @property
    def signed_amount(self) -> Decimal:
        """Debit-positive amount."""
        return self.amount if self.side is Side.DEBIT else -self.amount

    @property
    def signed_functional(self) -> Decimal:
        return self.functional_amount if self.side is Side.DEBIT else -self.functional_amount


@dataclass(frozen=True)
class QuantityMovement:
    """One change of an asset position, written to ``asset_quantity_ledger``.

    Quantities are signed (long positive, short negative). ``cost`` is the carried basis of
    the position in the asset currency: the cost of a long holding (1100) or the proceeds of
    a short position (2100), always non-negative.
    """

    asset_id: uuid.UUID
    movement_type: MovementType
    currency: str
    quantity_delta: Decimal
    quantity_after: Decimal
    cost_delta: Decimal
    cost_after: Decimal
    functional_cost_delta: Decimal
    functional_cost_after: Decimal
    price: Decimal | None
    realized_pnl: Decimal
    realized_pnl_functional: Decimal


@dataclass
class EntryDraft:
    entry_type: EntryType
    description: str
    lines: list[DraftLine] = field(default_factory=list)
    movements: list[QuantityMovement] = field(default_factory=list)
    # Structured facts about the posting (prices, rates, reliefs) stored with the entry.
    details: dict[str, object] = field(default_factory=dict)


def validate(draft: EntryDraft, functional_currency: str) -> None:
    """Raise :class:`EntryValidationError` unless the draft satisfies R1, R2 and line rules."""
    if len(draft.lines) < 2:
        raise EntryValidationError("a journal entry needs at least two lines")
    by_currency: dict[str, Decimal] = defaultdict(lambda: ZERO)
    functional_total = ZERO
    for number, line in enumerate(draft.lines, start=1):
        _validate_line(number, line, functional_currency)
        by_currency[line.currency] += line.signed_amount
        functional_total += line.signed_functional
    unbalanced = {c: total for c, total in by_currency.items() if total != ZERO}
    if unbalanced:
        detail = ", ".join(f"{c} {total:+}" for c, total in sorted(unbalanced.items()))
        raise EntryValidationError(f"R1: debits and credits differ per currency ({detail})")
    if functional_total != ZERO:
        raise EntryValidationError(
            f"R2: functional debits and credits differ by {functional_total:+} "
            f"{functional_currency}"
        )
    assets_moved = [m.asset_id for m in draft.movements]
    if len(assets_moved) != len(set(assets_moved)):
        raise EntryValidationError("an entry moves each asset at most once")


def _validate_line(number: int, line: DraftLine, functional_currency: str) -> None:
    where = f"line {number} ({line.account_code})"
    definition = CHART.get(line.account_code)
    if definition is None:
        raise EntryValidationError(f"{where}: unknown ledger account")
    try:
        spec = currency(line.currency)
    except MoneyError as exc:
        raise EntryValidationError(f"{where}: {exc}") from None
    if line.amount < ZERO or line.functional_amount < ZERO:
        raise EntryValidationError(f"{where}: amounts are non-negative; the side gives the sign")
    if scale_of(line.amount) > spec.minor_units:
        raise EntryValidationError(
            f"{where}: {line.amount} {line.currency} exceeds {spec.minor_units} decimal places"
        )
    places = currency(functional_currency).minor_units
    if scale_of(line.functional_amount) > places:
        raise EntryValidationError(f"{where}: functional value exceeds {places} decimal places")
    if line.fx_rate is not None:
        # A carrying or transaction rate is informational (functional / amount, rounded) and
        # may round to zero for a vanishing value; a spot or functional rate never does.
        informational = line.rate_basis in (RateBasis.CARRYING, RateBasis.TRANSACTION)
        if line.fx_rate < ZERO or (line.fx_rate == ZERO and not informational):
            raise EntryValidationError(f"{where}: the rate must be positive")
        if scale_of(line.fx_rate) > RATE_SCALE:
            raise EntryValidationError(f"{where}: the rate exceeds {RATE_SCALE} decimal places")

    if line.amount == ZERO:
        if not definition.allows_functional_only:
            raise EntryValidationError(f"{where}: zero-amount lines are not allowed here")
        if line.currency == functional_currency:
            raise EntryValidationError(
                f"{where}: a functional-only line relates to a foreign currency"
            )
        if line.functional_amount == ZERO:
            raise EntryValidationError(f"{where}: a line must carry an amount or a value")
    elif not definition.allows_amount:
        raise EntryValidationError(f"{where}: this account takes functional-only lines")

    if line.currency == functional_currency:
        if line.functional_amount != line.amount or line.fx_rate != ONE:
            raise EntryValidationError(
                f"{where}: a {functional_currency} line has functional value == amount, rate 1"
            )
        if line.rate_basis is not RateBasis.FUNCTIONAL or line.fx_rate_id is not None:
            raise EntryValidationError(f"{where}: functional-currency lines use no FX rate")
    elif line.rate_basis is RateBasis.FUNCTIONAL:
        raise EntryValidationError(f"{where}: a foreign-currency line needs a rate basis")
    if line.rate_basis is RateBasis.SPOT and line.fx_rate_id is None:
        raise EntryValidationError(f"{where}: a spot-rate line references its fx_rates row")
    if line.rate_basis is RateBasis.NONE and (line.fx_rate is not None or line.amount != ZERO):
        raise EntryValidationError(f"{where}: only functional-only lines have no rate")
    if line.rate_basis is not RateBasis.NONE and line.fx_rate is None:
        raise EntryValidationError(f"{where}: the rate used must be recorded")

    if definition.asset_rule is AssetRule.REQUIRED and line.asset_id is None:
        raise EntryValidationError(f"{where}: this account is kept per asset")
    if definition.asset_rule is AssetRule.FORBIDDEN and line.asset_id is not None:
        raise EntryValidationError(f"{where}: this account is not kept per asset")


__all__ = [
    "DraftLine",
    "EntryDraft",
    "EntryValidationError",
    "QuantityMovement",
    "RateBasis",
    "validate",
]

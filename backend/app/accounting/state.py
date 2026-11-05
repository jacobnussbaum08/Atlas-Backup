"""Ledger balances and positions of one portfolio account, as a working copy.

The posting service loads this from PostgreSQL under the account lock, a template builds a
draft against a copy, and the post-state invariants are checked on the copy before anything
is written. Tests build it in memory. Balances are kept per (ledger account, currency,
asset); amounts and functional values are debit-positive.
"""

from __future__ import annotations

import copy as _copy
import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.accounting.chart import (
    ACCRUED_FEES,
    ACCRUED_FINANCING,
    CASH,
    CHART,
    FX_TRANSLATION,
    LONG_COST,
    LONG_VALUATION,
    RESTRICTED_CASH,
    SHORT_PROCEEDS,
    SHORT_VALUATION,
)
from app.accounting.enums import Side
from app.accounting.ledger import DraftLine, QuantityMovement
from app.accounting.money import ZERO, functional_places, proportion

BalanceKey = tuple[str, str, uuid.UUID | None]


class InsufficientBalanceError(ValueError):
    """A posting would relieve more than a balance holds (e.g. spend cash that is not there)."""


@dataclass
class Balance:
    amount: Decimal = ZERO
    functional: Decimal = ZERO


@dataclass
class Position:
    asset_id: uuid.UUID
    currency: str
    quantity: Decimal = ZERO
    cost: Decimal = ZERO
    functional_cost: Decimal = ZERO

    @property
    def is_long(self) -> bool:
        return self.quantity > ZERO

    @property
    def is_short(self) -> bool:
        return self.quantity < ZERO


class LedgerState:
    def __init__(
        self,
        functional_currency: str,
        balances: dict[BalanceKey, Balance] | None = None,
        positions: dict[uuid.UUID, Position] | None = None,
    ) -> None:
        self.functional_currency = functional_currency
        self.balances: dict[BalanceKey, Balance] = balances or {}
        self.positions: dict[uuid.UUID, Position] = positions or {}

    def copy(self) -> LedgerState:
        return LedgerState(
            self.functional_currency,
            _copy.deepcopy(self.balances),
            _copy.deepcopy(self.positions),
        )

    # ------------------------------------------------------------------ reading
    def balance(self, code: str, currency: str, asset_id: uuid.UUID | None = None) -> Balance:
        return self.balances.get((code, currency, asset_id), Balance())

    def normal_balance(
        self, code: str, currency: str, asset_id: uuid.UUID | None = None
    ) -> tuple[Decimal, Decimal]:
        """(amount, functional) with the sign of the account's normal side."""
        found = self.balance(code, currency, asset_id)
        if CHART[code].normal_side is Side.DEBIT:
            return found.amount, found.functional
        return -found.amount, -found.functional

    def position(self, asset_id: uuid.UUID) -> Position | None:
        found = self.positions.get(asset_id)
        return found if found is not None and found.quantity != ZERO else None

    def currencies(self) -> set[str]:
        return {currency for (_, currency, _) in self.balances}

    def net_position(self, currency: str) -> tuple[Decimal, Decimal]:
        """Net balance-sheet amount in ``currency`` and its historical functional value.

        Assets minus liabilities, excluding functional-only accounts (the translation
        adjustment itself) and the FX clearing account (an equity account).
        """
        amount = functional = ZERO
        for (code, cur, _), bal in self.balances.items():
            definition = CHART[code]
            if cur != currency or not definition.is_balance_sheet or not definition.allows_amount:
                continue
            amount += bal.amount
            functional += bal.functional
        return amount, functional

    def translation(self, currency: str) -> Decimal:
        return self.balance(FX_TRANSLATION, currency).functional

    def relief_functional(
        self, code: str, currency: str, asset_id: uuid.UUID | None, amount: Decimal
    ) -> Decimal:
        """Functional value given up when ``amount`` leaves a balance (average carrying rate).

        Relieving the whole balance relieves all of its functional value, so an emptied
        balance never keeps a residue.
        """
        held, functional = self.normal_balance(code, currency, asset_id)
        if amount > held:
            raise InsufficientBalanceError(
                f"{CHART[code].name} holds {held} {currency}; cannot relieve {amount}"
            )
        if currency == self.functional_currency:
            return amount
        return proportion(functional, amount, held, functional_places(self.functional_currency))

    # ------------------------------------------------------------------ writing
    def apply_line(self, line: DraftLine) -> None:
        key = (line.account_code, line.currency, line.asset_id)
        current = self.balances.get(key, Balance())
        self.balances[key] = Balance(
            current.amount + line.signed_amount, current.functional + line.signed_functional
        )

    def apply_movement(self, movement: QuantityMovement) -> None:
        self.positions[movement.asset_id] = Position(
            asset_id=movement.asset_id,
            currency=movement.currency,
            quantity=movement.quantity_after,
            cost=movement.cost_after,
            functional_cost=movement.functional_cost_after,
        )


# Accounts whose normal-side balance can never be negative: cash cannot be overdrawn (no
# margin loan exists in the chart), and positions, collateral and payables cannot invert.
NON_NEGATIVE = (CASH, RESTRICTED_CASH, LONG_COST, SHORT_PROCEEDS, ACCRUED_FEES, ACCRUED_FINANCING)


def invariant_violations(state: LedgerState) -> list[str]:
    """Every way the state contradicts the accounting model (empty when consistent)."""
    problems: list[str] = []
    for (code, cur, asset), bal in sorted(
        state.balances.items(), key=lambda item: (item[0][0], item[0][1], str(item[0][2]))
    ):
        definition = CHART[code]
        held, functional = state.normal_balance(code, cur, asset)
        where = f"{code} {cur}" + (f" asset {asset}" if asset else "")
        if code in NON_NEGATIVE and held < ZERO:
            problems.append(f"{where}: negative balance {held}")
        if (
            definition.is_balance_sheet
            and definition.allows_amount
            and bal.amount == ZERO
            and bal.functional != ZERO
        ):
            problems.append(f"{where}: empty balance keeps functional value {functional}")
        if cur == state.functional_currency and bal.amount != bal.functional:
            problems.append(f"{where}: functional value differs from the functional amount")

    for asset_id, pos in state.positions.items():
        cost, fcost = state.normal_balance(LONG_COST, pos.currency, asset_id)
        proceeds, fproceeds = state.normal_balance(SHORT_PROCEEDS, pos.currency, asset_id)
        collateral, fcollateral = state.normal_balance(RESTRICTED_CASH, pos.currency, asset_id)
        long_adj = state.balance(LONG_VALUATION, pos.currency, asset_id).amount
        short_adj = state.balance(SHORT_VALUATION, pos.currency, asset_id).amount
        where = f"asset {asset_id}"
        if pos.quantity > ZERO:
            expected = (pos.cost, pos.functional_cost, ZERO, ZERO)
        elif pos.quantity < ZERO:
            expected = (ZERO, ZERO, pos.cost, pos.functional_cost)
        else:
            expected = (ZERO, ZERO, ZERO, ZERO)
            if pos.cost != ZERO or pos.functional_cost != ZERO:
                problems.append(f"{where}: flat position keeps a cost basis")
            if long_adj != ZERO or short_adj != ZERO:
                problems.append(f"{where}: flat position keeps a valuation adjustment")
        if (cost, fcost, proceeds, fproceeds) != expected:
            problems.append(f"{where}: ledger cost/proceeds differ from the quantity ledger")
        if pos.quantity >= ZERO and short_adj != ZERO:
            problems.append(f"{where}: short valuation adjustment without a short position")
        if pos.quantity <= ZERO and long_adj != ZERO:
            problems.append(f"{where}: long valuation adjustment without a long position")
        if (collateral, fcollateral) != (proceeds, fproceeds):
            problems.append(f"{where}: restricted proceeds differ from the short liability")
    for (code, cur, asset), bal in state.balances.items():
        if asset is None or code not in _POSITION_ACCOUNTS or asset in state.positions:
            continue
        if bal.amount != ZERO or bal.functional != ZERO:
            problems.append(
                f"{code} {cur} asset {asset}: balance without a quantity-ledger position"
            )
    return problems


_POSITION_ACCOUNTS = (LONG_COST, LONG_VALUATION, RESTRICTED_CASH, SHORT_PROCEEDS, SHORT_VALUATION)


__all__ = [
    "NON_NEGATIVE",
    "Balance",
    "BalanceKey",
    "InsufficientBalanceError",
    "LedgerState",
    "Position",
    "invariant_violations",
]

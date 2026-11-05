"""The chart of accounts (spec 16.2; decision D-127).

One system-wide chart. Every portfolio account posts to these ledger accounts; balances are
kept per portfolio account, ledger account, currency and (where the account requires it)
asset. The ``ledger_accounts`` table (migration 0013) mirrors this module exactly and is
append-only; a test keeps the two identical.

Line modes:

* ordinary accounts take lines with a positive amount in the line's currency and that
  amount's functional (reporting-currency) value;
* ``functional_only`` accounts (FX translation) take only lines with a zero currency amount
  and a non-zero functional value, in the foreign currency they relate to: they carry
  reporting-currency value attributable to a foreign currency, never currency itself;
* 4200 (realized FX) takes both: functional-only FX differences, and ordinary
  reporting-currency lines closing the FX clearing account after a conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.accounting.enums import AccountCategory, AccountType, Side


class AssetRule(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True)
class LedgerAccountDef:
    code: str
    name: str
    account_type: AccountType
    category: AccountCategory
    asset_rule: AssetRule
    allows_amount: bool
    allows_functional_only: bool
    description: str

    @property
    def normal_side(self) -> Side:
        return (
            Side.DEBIT
            if self.account_type in (AccountType.ASSET, AccountType.EXPENSE)
            else Side.CREDIT
        )

    @property
    def is_balance_sheet(self) -> bool:
        """Assets and liabilities: the items whose currency balances are translated."""
        return self.account_type in (AccountType.ASSET, AccountType.LIABILITY)


def _def(
    code: str,
    name: str,
    account_type: AccountType,
    category: AccountCategory,
    asset_rule: AssetRule,
    description: str,
    *,
    allows_amount: bool = True,
    allows_functional_only: bool = False,
) -> LedgerAccountDef:
    return LedgerAccountDef(
        code=code,
        name=name,
        account_type=account_type,
        category=category,
        asset_rule=asset_rule,
        allows_amount=allows_amount,
        allows_functional_only=allows_functional_only,
        description=description,
    )


A, L, E, INC, X = (
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
    AccountType.INCOME,
    AccountType.EXPENSE,
)
C = AccountCategory
REQ, OPT, NO = AssetRule.REQUIRED, AssetRule.OPTIONAL, AssetRule.FORBIDDEN

CASH = "1000"
RESTRICTED_CASH = "1010"
LONG_COST = "1100"
LONG_VALUATION = "1150"
FX_CLEARING = "1900"
FX_TRANSLATION = "1910"
SHORT_PROCEEDS = "2100"
SHORT_VALUATION = "2150"
ACCRUED_FEES = "2200"
ACCRUED_FINANCING = "2210"
CONTRIBUTED_CAPITAL = "3000"
REALIZED_PNL = "4000"
UNREALIZED_PNL = "4100"
REALIZED_FX = "4200"
UNREALIZED_FX = "4300"
COMMISSIONS = "5000"
FEES = "5100"
BORROW_COSTS = "5200"
FUNDING_COSTS = "5300"

CHART: dict[str, LedgerAccountDef] = {
    d.code: d
    for d in (
        _def(
            CASH,
            "Cash",
            A,
            C.CASH,
            NO,
            "Unrestricted cash, one balance per currency. The only source of available cash.",
        ),
        _def(
            RESTRICTED_CASH,
            "Restricted cash: short-sale proceeds",
            A,
            C.RESTRICTED_CASH,
            REQ,
            "Proceeds of open short sales, held as collateral against the short liability of "
            "the same asset. Not available cash and not equity.",
        ),
        _def(
            LONG_COST,
            "Long securities at cost",
            A,
            C.LONG_SECURITIES,
            REQ,
            "Cost basis of long holdings in the asset's currency (average cost; commissions "
            "and fees are expensed, not capitalised).",
        ),
        _def(
            LONG_VALUATION,
            "Long securities valuation adjustment",
            A,
            C.LONG_VALUATION,
            REQ,
            "Market value minus cost of each long holding, as of the last valuation "
            "checkpoint (adjunct to 1100; may be negative).",
        ),
        _def(
            FX_CLEARING,
            "FX conversion clearing",
            E,
            C.FX_CLEARING,
            NO,
            "Counterpart of currency conversions so each currency balances on its own. Its "
            "reporting-currency value is always zero: every conversion closes the difference "
            "to realized FX.",
        ),
        _def(
            FX_TRANSLATION,
            "FX translation adjustment",
            A,
            C.FX_TRANSLATION,
            NO,
            "Reporting-currency value of foreign-currency balance-sheet items above their "
            "historical functional carrying value, per currency (functional-only lines).",
            allows_amount=False,
            allows_functional_only=True,
        ),
        _def(
            SHORT_PROCEEDS,
            "Short securities liability at proceeds",
            L,
            C.SHORT_LIABILITY,
            REQ,
            "Obligation to return borrowed securities, carried at the short-sale "
            "proceeds (average proceeds per unit).",
        ),
        _def(
            SHORT_VALUATION,
            "Short securities valuation adjustment",
            L,
            C.SHORT_VALUATION,
            REQ,
            "Current cost to buy back each short position minus its carrying proceeds, as of "
            "the last valuation checkpoint (adjunct to 2100; may be negative).",
        ),
        _def(
            ACCRUED_FEES,
            "Accrued fees payable",
            L,
            C.ACCRUED_FEES,
            NO,
            "Fees and commissions incurred but not yet paid.",
        ),
        _def(
            ACCRUED_FINANCING,
            "Accrued financing payable",
            L,
            C.ACCRUED_FINANCING,
            NO,
            "Borrow and funding costs incurred but not yet paid.",
        ),
        _def(
            CONTRIBUTED_CAPITAL,
            "Contributed capital: simulated opening funding",
            E,
            C.CONTRIBUTED_CAPITAL,
            NO,
            "The simulated funding the paper account started with. Not an investment return.",
        ),
        _def(
            REALIZED_PNL,
            "Realized trading gains and losses",
            INC,
            C.REALIZED_PNL,
            OPT,
            "Proceeds minus cost basis on closed long quantity; carrying proceeds minus "
            "buy-back cost on covered short quantity.",
        ),
        _def(
            UNREALIZED_PNL,
            "Unrealized valuation changes",
            INC,
            C.UNREALIZED_PNL,
            OPT,
            "Change in the valuation adjustments (1150, 2150). Released when the quantity is "
            "closed, so it only ever describes open positions.",
        ),
        _def(
            REALIZED_FX,
            "Realized FX gains and losses",
            INC,
            C.REALIZED_FX,
            OPT,
            "Difference between the reporting-currency value received and the historical "
            "carrying value given up when foreign currency or a foreign-currency balance is "
            "realized.",
            allows_functional_only=True,
        ),
        _def(
            UNREALIZED_FX,
            "Unrealized FX translation",
            INC,
            C.UNREALIZED_FX,
            NO,
            "Change in the FX translation adjustment (1910), per currency (functional-only lines).",
            allows_amount=False,
            allows_functional_only=True,
        ),
        _def(
            COMMISSIONS, "Commissions", X, C.COMMISSIONS, OPT, "Brokerage commissions (simulated)."
        ),
        _def(
            FEES,
            "Fees",
            X,
            C.FEES,
            OPT,
            "Exchange, regulatory and other transaction fees (simulated).",
        ),
        _def(
            BORROW_COSTS,
            "Borrow costs",
            X,
            C.BORROW_COSTS,
            OPT,
            "Cost of borrowing securities for short positions.",
        ),
        _def(
            FUNDING_COSTS,
            "Funding costs",
            X,
            C.FUNDING_COSTS,
            OPT,
            "Financing of positions: forex rollover, crypto funding and similar.",
        ),
    )
}


def account(code: str) -> LedgerAccountDef:
    try:
        return CHART[code]
    except KeyError:
        raise KeyError(f"unknown ledger account {code!r}") from None


COST_ACCOUNTS = (COMMISSIONS, FEES, BORROW_COSTS, FUNDING_COSTS)

__all__ = [
    "ACCRUED_FEES",
    "ACCRUED_FINANCING",
    "BORROW_COSTS",
    "CASH",
    "CHART",
    "COMMISSIONS",
    "CONTRIBUTED_CAPITAL",
    "COST_ACCOUNTS",
    "FEES",
    "FUNDING_COSTS",
    "FX_CLEARING",
    "FX_TRANSLATION",
    "LONG_COST",
    "LONG_VALUATION",
    "REALIZED_FX",
    "REALIZED_PNL",
    "RESTRICTED_CASH",
    "SHORT_PROCEEDS",
    "SHORT_VALUATION",
    "UNREALIZED_FX",
    "UNREALIZED_PNL",
    "AssetRule",
    "LedgerAccountDef",
    "account",
]

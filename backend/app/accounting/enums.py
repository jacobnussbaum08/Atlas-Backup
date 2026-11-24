"""Enumerations of the accounting subsystem (Phase 6A). Stored as VARCHAR + CHECK."""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    """The five classical account types. Normal balance: ASSET/EXPENSE debit, others credit."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class Side(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"

    @property
    def opposite(self) -> Side:
        return Side.CREDIT if self is Side.DEBIT else Side.DEBIT


class AccountCategory(StrEnum):
    CASH = "CASH"
    RESTRICTED_CASH = "RESTRICTED_CASH"
    LONG_SECURITIES = "LONG_SECURITIES"
    LONG_VALUATION = "LONG_VALUATION"
    FX_CLEARING = "FX_CLEARING"
    FX_TRANSLATION = "FX_TRANSLATION"
    SHORT_LIABILITY = "SHORT_LIABILITY"
    SHORT_VALUATION = "SHORT_VALUATION"
    ACCRUED_FEES = "ACCRUED_FEES"
    ACCRUED_FINANCING = "ACCRUED_FINANCING"
    CONTRIBUTED_CAPITAL = "CONTRIBUTED_CAPITAL"
    REALIZED_PNL = "REALIZED_PNL"
    UNREALIZED_PNL = "UNREALIZED_PNL"
    REALIZED_FX = "REALIZED_FX"
    UNREALIZED_FX = "UNREALIZED_FX"
    COMMISSIONS = "COMMISSIONS"
    FEES = "FEES"
    BORROW_COSTS = "BORROW_COSTS"
    FUNDING_COSTS = "FUNDING_COSTS"


class EntryType(StrEnum):
    OPENING_FUNDING = "OPENING_FUNDING"
    LONG_PURCHASE = "LONG_PURCHASE"
    LONG_SALE = "LONG_SALE"
    SHORT_OPEN = "SHORT_OPEN"
    SHORT_COVER = "SHORT_COVER"
    FEE = "FEE"
    FINANCING_COST = "FINANCING_COST"
    ACCRUAL_SETTLEMENT = "ACCRUAL_SETTLEMENT"
    FX_CONVERSION = "FX_CONVERSION"
    VALUATION = "VALUATION"
    REVERSAL = "REVERSAL"


class EntrySource(StrEnum):
    """Where a posting came from."""

    # Opening funding and valuation checkpoints, posted by Atlas itself.
    SYSTEM = "SYSTEM"
    # Operator- or test-driven transactions on a CONTROLLED_TEST account; never on the
    # primary paper account (see PortfolioAccountPurpose).
    CONTROLLED_TEST = "CONTROLLED_TEST"
    # A correcting reversal requested by an operator.
    OPERATOR = "OPERATOR"
    # Phase 7B: a simulated paper fill (or its execution cost: commission, fees, borrow)
    # derived by the isolated paper broker from stored market data under a versioned
    # execution model. Simulated execution, never a brokerage execution.
    SIMULATED_EXECUTION = "SIMULATED_EXECUTION"


class MovementType(StrEnum):
    """Asset quantity ledger movements."""

    LONG_INCREASE = "LONG_INCREASE"
    LONG_DECREASE = "LONG_DECREASE"
    SHORT_INCREASE = "SHORT_INCREASE"
    SHORT_DECREASE = "SHORT_DECREASE"
    REVERSAL = "REVERSAL"


class FeeKind(StrEnum):
    COMMISSION = "COMMISSION"
    FEE = "FEE"


class FinancingKind(StrEnum):
    BORROW = "BORROW"
    FUNDING = "FUNDING"


class PayableKind(StrEnum):
    FEES = "FEES"
    FINANCING = "FINANCING"


class FxRateSource(StrEnum):
    """Provenance class of an FX rate. TEST_FIXTURE rates exist only in tests."""

    PROVIDER = "PROVIDER"
    OPERATOR = "OPERATOR"
    CONVERSION = "CONVERSION"
    TEST_FIXTURE = "TEST_FIXTURE"


class InstrumentSpecSource(StrEnum):
    PROVIDER_REFERENCE = "PROVIDER_REFERENCE"
    OPERATOR = "OPERATOR"


class PriceStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


class ValuationStatus(StrEnum):
    """How complete a portfolio snapshot's valuation is.

    COMPLETE: every open position and foreign balance was valued from a fresh price/rate.
    STALE: at least one input was stale; that item kept its last posted valuation.
    INCOMPLETE: at least one price or rate was missing; that item kept its last valuation.
    """

    COMPLETE = "COMPLETE"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"


class ReconciliationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class CheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105  # a status value, not a credential
    FAIL = "FAIL"


class Severity(StrEnum):
    INFO = "INFO"
    HIGH = "HIGH"

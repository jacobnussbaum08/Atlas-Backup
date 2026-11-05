"""Portfolio figures derived from ledger balances (decision D-134).

The accounting equation, in the functional (reporting) currency:

    total assets - total liabilities = equity
    equity = contributed capital + realized P/L + unrealized valuation changes
             + realized FX + unrealized FX translation
             - commissions - fees - borrow costs - funding costs

where

    total assets      = cash (1000) + restricted short proceeds (1010)
                        + long securities at cost (1100) + their valuation adjustment (1150)
                        + FX translation adjustment (1910)
    total liabilities = short liability at proceeds (2100) + its valuation adjustment (2150)
                        + accrued fees (2200) + accrued financing (2210)

Both sides come from the same posted lines, so they agree whenever the trial balance does;
reconciliation checks it. Short-sale proceeds are an asset (restricted cash) matched by the
short liability: they add nothing to equity and nothing to available cash.

Investment return = equity - contributed capital: the simulated opening funding is never a
return. Available capital = unrestricted cash - accrued unpaid costs (at carrying value; no
margin or risk reservations exist yet, Phase 6B).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from app.accounting import chart
from app.accounting.chart import CHART
from app.accounting.enums import AccountType
from app.accounting.money import ZERO
from app.accounting.state import LedgerState


def _functional(state: LedgerState, *codes: str) -> Decimal:
    """Debit-positive functional total of the given accounts (all currencies and assets)."""
    return sum((b.functional for (code, _, _), b in state.balances.items() if code in codes), ZERO)


@dataclass(frozen=True)
class PortfolioFigures:
    functional_currency: str
    contributed_capital: Decimal
    cash: Decimal
    restricted_cash: Decimal
    long_value: Decimal
    short_liability: Decimal
    accrued_costs: Decimal
    fx_translation: Decimal
    total_assets: Decimal
    total_liabilities: Decimal
    equity: Decimal
    equity_by_components: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    realized_fx: Decimal
    unrealized_fx: Decimal
    commissions: Decimal
    fees: Decimal
    borrow_costs: Decimal
    funding_costs: Decimal
    total_costs: Decimal
    fx_clearing: Decimal
    investment_return: Decimal
    available_capital: Decimal
    long_exposure: Decimal
    short_exposure: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def figures(state: LedgerState) -> PortfolioFigures:
    f = state
    cash = _functional(f, chart.CASH)
    restricted = _functional(f, chart.RESTRICTED_CASH)
    long_value = _functional(f, chart.LONG_COST, chart.LONG_VALUATION)
    short_liability = -_functional(f, chart.SHORT_PROCEEDS, chart.SHORT_VALUATION)
    accrued = -_functional(f, chart.ACCRUED_FEES, chart.ACCRUED_FINANCING)
    translation = _functional(f, chart.FX_TRANSLATION)
    assets = sum(
        (
            b.functional
            for (c, _, _), b in f.balances.items()
            if CHART[c].account_type is AccountType.ASSET
        ),
        ZERO,
    )
    liabilities = -sum(
        (
            b.functional
            for (c, _, _), b in f.balances.items()
            if CHART[c].account_type is AccountType.LIABILITY
        ),
        ZERO,
    )
    capital = -_functional(f, chart.CONTRIBUTED_CAPITAL)
    realized = -_functional(f, chart.REALIZED_PNL)
    unrealized = -_functional(f, chart.UNREALIZED_PNL)
    realized_fx = -_functional(f, chart.REALIZED_FX)
    unrealized_fx = -_functional(f, chart.UNREALIZED_FX)
    commissions = _functional(f, chart.COMMISSIONS)
    fees = _functional(f, chart.FEES)
    borrow = _functional(f, chart.BORROW_COSTS)
    funding = _functional(f, chart.FUNDING_COSTS)
    costs = commissions + fees + borrow + funding
    clearing = -_functional(f, chart.FX_CLEARING)
    equity = assets - liabilities
    by_components = capital + realized + unrealized + realized_fx + unrealized_fx - costs + clearing
    return PortfolioFigures(
        functional_currency=f.functional_currency,
        contributed_capital=capital,
        cash=cash,
        restricted_cash=restricted,
        long_value=long_value,
        short_liability=short_liability,
        accrued_costs=accrued,
        fx_translation=translation,
        total_assets=assets,
        total_liabilities=liabilities,
        equity=equity,
        equity_by_components=by_components,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        realized_fx=realized_fx,
        unrealized_fx=unrealized_fx,
        commissions=commissions,
        fees=fees,
        borrow_costs=borrow,
        funding_costs=funding,
        total_costs=costs,
        fx_clearing=clearing,
        investment_return=equity - capital,
        available_capital=cash - accrued,
        long_exposure=long_value,
        short_exposure=short_liability,
        gross_exposure=long_value + short_liability,
        net_exposure=long_value - short_liability,
    )


def cash_by_currency(state: LedgerState) -> list[dict[str, str]]:
    """Per currency: unrestricted and restricted cash, accrued payables and carrying values."""
    rows: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "cash": ZERO,
            "cash_functional": ZERO,
            "restricted": ZERO,
            "restricted_functional": ZERO,
            "accrued_payables": ZERO,
            "translation_functional": ZERO,
        }
    )
    for (code, cur, _), bal in state.balances.items():
        row = rows[cur]
        if code == chart.CASH:
            row["cash"] += bal.amount
            row["cash_functional"] += bal.functional
        elif code == chart.RESTRICTED_CASH:
            row["restricted"] += bal.amount
            row["restricted_functional"] += bal.functional
        elif code in (chart.ACCRUED_FEES, chart.ACCRUED_FINANCING):
            row["accrued_payables"] -= bal.amount
        elif code == chart.FX_TRANSLATION:
            row["translation_functional"] += bal.functional
    return [
        {"currency": cur, **{k: str(v) for k, v in values.items()}}
        for cur, values in sorted(rows.items())
        if any(v != ZERO for v in values.values())
    ]


__all__ = ["PortfolioFigures", "cash_by_currency", "figures"]

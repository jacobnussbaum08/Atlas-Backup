"""Portfolio and accounting API (Phase 6A). Read-only.

``/portfolio``: accounts, overview (equity, cash, exposure, P/L, valuation and reconciliation
status), cash by currency, holdings, P/L breakdown, snapshots.
``/accounting``: chart of accounts, posting templates, journal history and entries, account
balances (trial balance), reconciliation runs and results, FX rates.

Nothing here posts, funds, values or reconciles: those are CLI jobs
(``python -m app.accounting.cli``). No endpoint creates, sizes, approves or submits an order.
Without ``account_id`` the primary paper account is used.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.accounting.enums import EntryType
from app.accounting.runtime import AccountingContext
from app.core.exceptions import DependencyUnavailableError
from app.database.sessions import session_scope
from app.portfolio import queries

portfolio_router = APIRouter(prefix="/portfolio", tags=["portfolio"])
accounting_router = APIRouter(prefix="/accounting", tags=["accounting"])
_lock = threading.Lock()


def accounting(request: Request) -> AccountingContext:
    """The accounting context, built on first use."""
    state = request.app.state
    context: AccountingContext | None = getattr(state, "accounting", None)
    if context is None:
        factory = getattr(state, "accounting_factory", None)
        if factory is None:
            raise DependencyUnavailableError("accounting is not configured")
        with _lock:
            context = getattr(state, "accounting", None)
            if context is None:
                context = factory()
                state.accounting = context
    return context


Context = Annotated[AccountingContext, Depends(accounting)]
AccountId = Annotated[uuid.UUID | None, Query(description="Defaults to the primary account")]
Limit = Annotated[int, Query(ge=1, le=200)]


# ------------------------------------------------------------------ portfolio
@portfolio_router.get("/accounts", response_model=list[queries.AccountView])
def list_accounts(ctx: Context) -> list[queries.AccountView]:
    with session_scope(ctx.session_factory) as session:
        return queries.accounts(session)


@portfolio_router.get("/overview", response_model=queries.Overview)
def overview(ctx: Context, account_id: AccountId = None) -> queries.Overview:
    with session_scope(ctx.session_factory) as session:
        return queries.overview(session, queries.resolve_account(session, account_id))


@portfolio_router.get("/cash")
def cash(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        account = queries.resolve_account(session, account_id)
        view = queries.overview(session, account)
        return {
            "account_id": str(account.id),
            "functional_currency": account.base_currency,
            "currencies": view.cash_by_currency,
            "available_cash": str(view.figures["cash"]),
            "available_capital": str(view.figures["available_capital"]),
            "restricted_cash": str(view.figures["restricted_cash"]),
            "note": (
                "Restricted cash is short-sale proceeds held as collateral: it is neither "
                "available cash nor equity. Functional values are historical carrying values; "
                "the FX translation adjustment is reported separately."
            ),
        }


@portfolio_router.get("/holdings", response_model=list[queries.HoldingView])
def holdings(ctx: Context, account_id: AccountId = None) -> list[queries.HoldingView]:
    with session_scope(ctx.session_factory) as session:
        return queries.holdings(session, queries.resolve_account(session, account_id))


@portfolio_router.get("/pnl")
def pnl(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.pnl(session, queries.resolve_account(session, account_id))


@portfolio_router.get("/snapshots")
def snapshots(
    ctx: Context, account_id: AccountId = None, limit: Limit = 50
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.snapshots(session, queries.resolve_account(session, account_id), limit)


# ------------------------------------------------------------------ accounting
@accounting_router.get("/chart")
def chart(ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.chart_of_accounts(session)


@accounting_router.get("/templates")
def templates(ctx: Context) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.posting_templates(session)


@accounting_router.get("/journal", response_model=list[queries.EntrySummary])
def journal(
    ctx: Context,
    account_id: AccountId = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    entry_type: EntryType | None = None,
    limit: Limit = 50,
) -> list[queries.EntrySummary]:
    with session_scope(ctx.session_factory) as session:
        return queries.journal(
            session,
            queries.resolve_account(session, account_id),
            before=before,
            entry_type=entry_type,
            limit=limit,
        )


@accounting_router.get("/journal/{entry_id}", response_model=queries.EntryDetail)
def journal_entry(ctx: Context, entry_id: uuid.UUID) -> queries.EntryDetail:
    with session_scope(ctx.session_factory) as session:
        return queries.entry(session, entry_id)


@accounting_router.get("/balances", response_model=queries.TrialBalance)
def balances(ctx: Context, account_id: AccountId = None) -> queries.TrialBalance:
    with session_scope(ctx.session_factory) as session:
        return queries.trial_balance(session, queries.resolve_account(session, account_id))


@accounting_router.get("/reconciliation", response_model=list[queries.RunSummaryView])
def reconciliation(
    ctx: Context, account_id: AccountId = None, limit: Limit = 20
) -> list[queries.RunSummaryView]:
    with session_scope(ctx.session_factory) as session:
        return queries.reconciliation_runs(
            session, queries.resolve_account(session, account_id), limit
        )


@accounting_router.get("/reconciliation/{run_id}")
def reconciliation_run(ctx: Context, run_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.reconciliation_run(session, run_id)


@accounting_router.get("/fx-rates")
def fx_rates(ctx: Context, limit: Limit = 100) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.fx_rates(session, limit)


__all__ = ["accounting_router", "portfolio_router"]

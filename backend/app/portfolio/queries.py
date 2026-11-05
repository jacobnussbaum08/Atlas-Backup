"""Read models for the portfolio and accounting API (Phase 6A). Read-only.

Every figure is derived from posted journal lines and the quantity ledger (or a stored
snapshot, which says so). Amounts are Decimals serialised as strings.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import chart
from app.accounting.chart import CHART
from app.accounting.enums import CheckStatus, EntryType, Side
from app.accounting.posting import last_sequence, load_state
from app.core.exceptions import NotFoundError
from app.database.models import Asset, PortfolioAccount
from app.database.models.accounting import (
    AssetQuantityRecord,
    FxRateRecord,
    JournalEntryRecord,
    JournalLineRecord,
    LedgerAccountRecord,
    PostingTemplateRecord,
    ReconciliationResultRecord,
    ReconciliationRunRecord,
)
from app.database.models.portfolio import PortfolioSnapshotPositionRecord, PortfolioSnapshotRecord
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.portfolio.figures import cash_by_currency, figures

ZERO = Decimal(0)
NOT_INITIALIZED = (
    "no primary paper account exists yet: run `python -m app.accounting.cli init` to create "
    "it with its simulated opening funding"
)
DISCLAIMER = (
    "Simulated paper account. Opening funding is simulated contributed capital, not a return; "
    "no order has been executed and no broker is connected."
)


class AccountView(BaseModel):
    id: uuid.UUID
    name: str
    purpose: str
    base_currency: str
    status: str
    created_at: datetime
    entries: int
    opening_funding: Decimal | None
    opening_funding_posted_at: datetime | None


class SnapshotSummary(BaseModel):
    id: uuid.UUID
    as_of: datetime
    valuation_status: str
    entry_sequence: int
    entries_since: int
    equity: Decimal
    issues: list[dict[str, Any]]


class RunSummaryView(BaseModel):
    id: uuid.UUID
    status: str
    checks_total: int
    checks_failed: int
    entry_sequence: int
    current: bool
    trigger: str
    finished_at: datetime


class ResultView(BaseModel):
    check_code: str
    scope: str
    currency: str | None
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    status: str
    severity: str
    message: str


class Overview(BaseModel):
    account: AccountView
    figures: dict[str, Any]
    cash_by_currency: list[dict[str, str]]
    positions: dict[str, int]
    latest_snapshot: SnapshotSummary | None
    reconciliation: RunSummaryView | None
    accounting_errors: list[ResultView]
    disclaimer: str


class LineView(BaseModel):
    line_number: int
    account_code: str
    account_name: str
    side: str
    currency: str
    debit: Decimal
    credit: Decimal
    functional_debit: Decimal
    functional_credit: Decimal
    rate_basis: str
    fx_rate: Decimal | None
    fx_rate_id: int | None
    asset_id: uuid.UUID | None
    symbol: str | None
    memo: str | None


class MovementView(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    movement_type: str
    currency: str
    quantity_delta: Decimal
    quantity_after: Decimal
    cost_delta: Decimal
    cost_after: Decimal
    price: Decimal | None
    realized_pnl: Decimal
    realized_pnl_functional: Decimal


class EntrySummary(BaseModel):
    id: uuid.UUID
    sequence: int
    event_id: str
    entry_type: str
    source: str
    source_ref: str | None
    template: str
    effective_at: datetime
    posted_at: datetime
    description: str
    functional_currency: str
    functional_total: Decimal
    line_count: int
    reverses_entry_id: uuid.UUID | None
    reversed_by_entry_id: uuid.UUID | None


class EntryDetail(EntrySummary):
    payload: dict[str, Any]
    details: dict[str, Any]
    lines: list[LineView]
    movements: list[MovementView]


class BalanceRow(BaseModel):
    account_code: str
    account_name: str
    account_type: str
    normal_side: str
    currency: str
    debit_total: Decimal
    credit_total: Decimal
    balance: Decimal
    functional_balance: Decimal


class TrialBalance(BaseModel):
    rows: list[BalanceRow]
    by_currency: dict[str, dict[str, Decimal]]
    functional: dict[str, Decimal]
    balanced: bool


class HoldingView(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    currency: str
    side: str
    quantity: Decimal
    cost_basis: Decimal
    average_price: Decimal
    valuation_adjustment: Decimal
    carried_value: Decimal
    carried_value_functional: Decimal
    realized_pnl: Decimal
    last_price: Decimal | None
    last_price_status: str | None
    last_price_source: str | None
    last_price_observed_at: datetime | None
    last_price_provenance: dict[str, Any] | None
    valued_at: datetime | None


# ------------------------------------------------------------------ accounts
def resolve_account(session: Session, account_id: uuid.UUID | None) -> PortfolioAccount:
    if account_id is not None:
        found = session.get(PortfolioAccount, account_id)
        if found is None:
            raise NotFoundError(f"portfolio account {account_id} does not exist")
        return found
    primary = session.scalar(
        select(PortfolioAccount)
        .where(PortfolioAccount.purpose == PortfolioAccountPurpose.PRIMARY)
        .order_by(PortfolioAccount.created_at, PortfolioAccount.name)
        .limit(1)
    )
    if primary is None:
        raise NotFoundError(NOT_INITIALIZED)
    return primary


def _account_view(session: Session, account: PortfolioAccount) -> AccountView:
    funding = session.scalar(
        select(JournalEntryRecord).where(
            JournalEntryRecord.portfolio_account_id == account.id,
            JournalEntryRecord.entry_type == EntryType.OPENING_FUNDING,
        )
    )
    return AccountView(
        id=account.id,
        name=account.name,
        purpose=account.purpose.value,
        base_currency=account.base_currency,
        status=account.status.value,
        created_at=account.created_at,
        entries=last_sequence(session, account.id),
        opening_funding=Decimal(str(funding.payload["amount"])) if funding else None,
        opening_funding_posted_at=funding.posted_at if funding else None,
    )


def accounts(session: Session) -> list[AccountView]:
    rows = session.scalars(
        select(PortfolioAccount).order_by(PortfolioAccount.purpose.desc(), PortfolioAccount.name)
    ).all()
    return [_account_view(session, a) for a in rows]


# ------------------------------------------------------------------ overview
def _latest_snapshot(session: Session, account_id: uuid.UUID) -> PortfolioSnapshotRecord | None:
    return session.scalar(
        select(PortfolioSnapshotRecord)
        .where(PortfolioSnapshotRecord.portfolio_account_id == account_id)
        .order_by(PortfolioSnapshotRecord.as_of.desc())
        .limit(1)
    )


def _latest_run(session: Session, account_id: uuid.UUID) -> ReconciliationRunRecord | None:
    return session.scalar(
        select(ReconciliationRunRecord)
        .where(ReconciliationRunRecord.portfolio_account_id == account_id)
        .order_by(ReconciliationRunRecord.finished_at.desc())
        .limit(1)
    )


def _run_view(run: ReconciliationRunRecord, sequence: int) -> RunSummaryView:
    return RunSummaryView(
        id=run.id,
        status=run.status.value,
        checks_total=run.checks_total,
        checks_failed=run.checks_failed,
        entry_sequence=run.entry_sequence,
        current=run.entry_sequence == sequence,
        trigger=run.trigger,
        finished_at=run.finished_at,
    )


def _result_view(r: ReconciliationResultRecord) -> ResultView:
    return ResultView(
        check_code=r.check_code,
        scope=r.scope,
        currency=r.currency,
        expected=r.expected,
        actual=r.actual,
        difference=r.difference,
        status=r.status.value,
        severity=r.severity.value,
        message=r.message,
    )


def overview(session: Session, account: PortfolioAccount) -> Overview:
    state = load_state(session, account.id, account.base_currency)
    sequence = last_sequence(session, account.id)
    snap = _latest_snapshot(session, account.id)
    run = _latest_run(session, account.id)
    errors: list[ResultView] = []
    if run is not None:
        errors = [
            _result_view(r)
            for r in session.scalars(
                select(ReconciliationResultRecord)
                .where(
                    ReconciliationResultRecord.run_id == run.id,
                    ReconciliationResultRecord.status == CheckStatus.FAIL,
                )
                .order_by(ReconciliationResultRecord.id)
            )
        ]
    open_positions = [p for p in state.positions.values() if p.quantity != ZERO]
    return Overview(
        account=_account_view(session, account),
        figures={
            k: format(v, "f") if isinstance(v, Decimal) else v
            for k, v in figures(state).as_dict().items()
        },
        cash_by_currency=cash_by_currency(state),
        positions={
            "long": sum(1 for p in open_positions if p.quantity > ZERO),
            "short": sum(1 for p in open_positions if p.quantity < ZERO),
        },
        latest_snapshot=SnapshotSummary(
            id=snap.id,
            as_of=snap.as_of,
            valuation_status=snap.valuation_status.value,
            entry_sequence=snap.entry_sequence,
            entries_since=sequence - snap.entry_sequence,
            equity=snap.equity,
            issues=snap.issues,
        )
        if snap is not None
        else None,
        reconciliation=_run_view(run, sequence) if run is not None else None,
        accounting_errors=errors,
        disclaimer=DISCLAIMER,
    )


# ------------------------------------------------------------------ holdings and P/L
def _symbols(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, Asset]:
    if not ids:
        return {}
    return {a.id: a for a in session.scalars(select(Asset).where(Asset.id.in_(ids)))}


def holdings(session: Session, account: PortfolioAccount) -> list[HoldingView]:
    state = load_state(session, account.id, account.base_currency)
    open_positions = {a: p for a, p in state.positions.items() if p.quantity != ZERO}
    assets = _symbols(session, set(open_positions))
    realized = {
        asset: Decimal(total)
        for asset, total in session.execute(
            select(AssetQuantityRecord.asset_id, func.sum(AssetQuantityRecord.realized_pnl))
            .where(AssetQuantityRecord.portfolio_account_id == account.id)
            .group_by(AssetQuantityRecord.asset_id)
        )
    }
    snap = _latest_snapshot(session, account.id)
    marks: dict[uuid.UUID, PortfolioSnapshotPositionRecord] = {}
    if snap is not None:
        marks = {
            p.asset_id: p
            for p in session.scalars(
                select(PortfolioSnapshotPositionRecord).where(
                    PortfolioSnapshotPositionRecord.snapshot_id == snap.id
                )
            )
        }
    out: list[HoldingView] = []
    for asset_id, pos in sorted(open_positions.items(), key=lambda i: assets[i[0]].symbol):
        asset = assets[asset_id]
        if pos.quantity > ZERO:
            adj = state.balance(chart.LONG_VALUATION, pos.currency, asset_id)
            carried = pos.cost + adj.amount
            adjustment = adj.amount
            functional = (
                state.balance(chart.LONG_COST, pos.currency, asset_id).functional + adj.functional
            )
        else:
            adj = state.balance(chart.SHORT_VALUATION, pos.currency, asset_id)
            carried = pos.cost - adj.amount
            adjustment = -adj.amount
            functional = -(
                state.balance(chart.SHORT_PROCEEDS, pos.currency, asset_id).functional
                + adj.functional
            )
        mark = marks.get(asset_id)
        out.append(
            HoldingView(
                asset_id=asset_id,
                symbol=asset.symbol,
                asset_class=asset.asset_class.value,
                currency=pos.currency,
                side="LONG" if pos.quantity > ZERO else "SHORT",
                quantity=pos.quantity,
                cost_basis=pos.cost,
                average_price=(pos.cost / abs(pos.quantity)).quantize(Decimal("1e-10")),
                valuation_adjustment=adjustment,
                carried_value=carried,
                carried_value_functional=functional,
                realized_pnl=realized.get(asset_id, ZERO),
                last_price=mark.price if mark else None,
                last_price_status=mark.price_status.value if mark else None,
                last_price_source=mark.price_source if mark else None,
                last_price_observed_at=mark.price_observed_at if mark else None,
                last_price_provenance=mark.price_provenance if mark else None,
                valued_at=snap.as_of if mark and snap else None,
            )
        )
    return out


def pnl(session: Session, account: PortfolioAccount) -> dict[str, Any]:
    """Realized and unrealized P/L, FX and costs, in total and per asset and currency."""
    state = load_state(session, account.id, account.base_currency)
    totals = figures(state)
    per_asset: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    fx_by_currency: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    asset_currency: dict[uuid.UUID, str] = {}
    names = {
        chart.REALIZED_PNL: "realized",
        chart.UNREALIZED_PNL: "unrealized",
        chart.REALIZED_FX: "realized_fx",
        chart.COMMISSIONS: "commissions",
        chart.FEES: "fees",
        chart.BORROW_COSTS: "borrow_costs",
        chart.FUNDING_COSTS: "funding_costs",
    }
    for (code, cur, asset), bal in state.balances.items():
        sign = -1 if CHART[code].normal_side is Side.CREDIT else 1
        if code in (chart.REALIZED_FX, chart.UNREALIZED_FX):
            fx_by_currency[cur][names.get(code, "unrealized_fx")] += sign * bal.functional
        if asset is not None and code in names:
            row = per_asset[asset]
            row[f"{names[code]}_functional"] += sign * bal.functional
            if bal.amount != ZERO:
                row[names[code]] += sign * bal.amount
            asset_currency[asset] = cur
    assets = _symbols(session, set(per_asset))
    by_asset = []
    for asset_id, values in sorted(per_asset.items(), key=lambda i: assets[i[0]].symbol):
        by_asset.append(
            {
                "asset_id": str(asset_id),
                "symbol": assets[asset_id].symbol,
                "currency": asset_currency[asset_id],
                **{k: str(v) for k, v in sorted(values.items())},
            }
        )
    unattributed = {
        name: str(
            sum(
                (
                    (-1 if CHART[code].normal_side is Side.CREDIT else 1) * b.functional
                    for (c, _, a), b in state.balances.items()
                    if c == code and a is None
                ),
                ZERO,
            )
        )
        for code, name in names.items()
    }
    return {
        "functional_currency": account.base_currency,
        "totals": {
            k: str(getattr(totals, k))
            for k in (
                "realized_pnl",
                "unrealized_pnl",
                "realized_fx",
                "unrealized_fx",
                "commissions",
                "fees",
                "borrow_costs",
                "funding_costs",
                "total_costs",
                "investment_return",
                "contributed_capital",
                "equity",
            )
        },
        "by_asset": by_asset,
        "unattributed": unattributed,
        "fx_by_currency": {
            cur: {k: str(v) for k, v in values.items()}
            for cur, values in sorted(fx_by_currency.items())
        },
        "note": (
            "Realized P/L is proceeds minus average cost on closed quantity; unrealized is the "
            "posted valuation of open quantity as of the last checkpoint. Opening funding is "
            "contributed capital and is excluded from every return."
        ),
    }


# ------------------------------------------------------------------ journal
def _entry_summary(
    e: JournalEntryRecord, template: str, lines: int, total: Decimal, reversed_by: uuid.UUID | None
) -> EntrySummary:
    return EntrySummary(
        id=e.id,
        sequence=e.sequence,
        event_id=e.event_id,
        entry_type=e.entry_type.value,
        source=e.source.value,
        source_ref=e.source_ref,
        template=template,
        effective_at=e.effective_at,
        posted_at=e.posted_at,
        description=e.description,
        functional_currency=e.functional_currency,
        functional_total=total,
        line_count=lines,
        reverses_entry_id=e.reverses_entry_id,
        reversed_by_entry_id=reversed_by,
    )


def journal(
    session: Session,
    account: PortfolioAccount,
    *,
    before: int | None = None,
    entry_type: EntryType | None = None,
    limit: int = 50,
) -> list[EntrySummary]:
    stmt = (
        select(JournalEntryRecord)
        .where(JournalEntryRecord.portfolio_account_id == account.id)
        .order_by(JournalEntryRecord.sequence.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(JournalEntryRecord.sequence < before)
    if entry_type is not None:
        stmt = stmt.where(JournalEntryRecord.entry_type == entry_type)
    entries = session.scalars(stmt).all()
    return _summaries(session, entries)


def _summaries(session: Session, entries: Any) -> list[EntrySummary]:
    ids = [e.id for e in entries]
    if not ids:
        return []
    stats = {
        entry_id: (int(n), Decimal(total))
        for entry_id, n, total in session.execute(
            select(
                JournalLineRecord.entry_id,
                func.count(),
                func.sum(JournalLineRecord.functional_amount).filter(
                    JournalLineRecord.side == Side.DEBIT
                ),
            )
            .where(JournalLineRecord.entry_id.in_(ids))
            .group_by(JournalLineRecord.entry_id)
        )
    }
    reversals = {
        r.reverses_entry_id: r.id
        for r in session.scalars(
            select(JournalEntryRecord).where(JournalEntryRecord.reverses_entry_id.in_(ids))
        )
    }
    templates = {
        t.id: f"{t.code}.v{t.version}" for t in session.scalars(select(PostingTemplateRecord))
    }
    return [
        _entry_summary(
            e,
            templates.get(e.template_id, "?"),
            stats.get(e.id, (0, ZERO))[0],
            stats.get(e.id, (0, ZERO))[1],
            reversals.get(e.id),
        )
        for e in entries
    ]


def entry(session: Session, entry_id: uuid.UUID) -> EntryDetail:
    e = session.get(JournalEntryRecord, entry_id)
    if e is None:
        raise NotFoundError(f"journal entry {entry_id} does not exist")
    summary = _summaries(session, [e])[0]
    lines = session.scalars(
        select(JournalLineRecord)
        .where(JournalLineRecord.entry_id == entry_id)
        .order_by(JournalLineRecord.line_number)
    ).all()
    movements = session.scalars(
        select(AssetQuantityRecord).where(AssetQuantityRecord.entry_id == entry_id)
    ).all()
    assets = _symbols(
        session,
        {line.asset_id for line in lines if line.asset_id} | {m.asset_id for m in movements},
    )
    return EntryDetail(
        **summary.model_dump(),
        payload=e.payload,
        details=e.details,
        lines=[
            LineView(
                line_number=line.line_number,
                account_code=line.account_code,
                account_name=CHART[line.account_code].name,
                side=line.side.value,
                currency=line.currency,
                debit=line.amount if line.side is Side.DEBIT else ZERO,
                credit=line.amount if line.side is Side.CREDIT else ZERO,
                functional_debit=line.functional_amount if line.side is Side.DEBIT else ZERO,
                functional_credit=line.functional_amount if line.side is Side.CREDIT else ZERO,
                rate_basis=line.rate_basis.value,
                fx_rate=line.fx_rate,
                fx_rate_id=line.fx_rate_id,
                asset_id=line.asset_id,
                symbol=assets[line.asset_id].symbol if line.asset_id in assets else None,
                memo=line.memo,
            )
            for line in lines
        ],
        movements=[
            MovementView(
                asset_id=m.asset_id,
                symbol=assets[m.asset_id].symbol,
                movement_type=m.movement_type.value,
                currency=m.currency,
                quantity_delta=m.quantity_delta,
                quantity_after=m.quantity_after,
                cost_delta=m.cost_delta,
                cost_after=m.cost_after,
                price=m.price,
                realized_pnl=m.realized_pnl,
                realized_pnl_functional=m.realized_pnl_functional,
            )
            for m in movements
        ],
    )


# ------------------------------------------------------------------ balances
def trial_balance(session: Session, account: PortfolioAccount) -> TrialBalance:
    debit = JournalLineRecord.side == Side.DEBIT
    rows = session.execute(
        select(
            JournalLineRecord.account_code,
            JournalLineRecord.currency,
            func.coalesce(func.sum(JournalLineRecord.amount).filter(debit), 0),
            func.coalesce(func.sum(JournalLineRecord.amount).filter(~debit), 0),
            func.coalesce(func.sum(JournalLineRecord.functional_amount).filter(debit), 0),
            func.coalesce(func.sum(JournalLineRecord.functional_amount).filter(~debit), 0),
        )
        .where(JournalLineRecord.portfolio_account_id == account.id)
        .group_by(JournalLineRecord.account_code, JournalLineRecord.currency)
        .order_by(JournalLineRecord.account_code, JournalLineRecord.currency)
    ).all()
    out: list[BalanceRow] = []
    by_currency: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"debits": ZERO, "credits": ZERO}
    )
    functional = {"debits": ZERO, "credits": ZERO}
    for code, cur, dr, cr, fdr, fcr in rows:
        definition = CHART[code]
        sign = 1 if definition.normal_side is Side.DEBIT else -1
        out.append(
            BalanceRow(
                account_code=code,
                account_name=definition.name,
                account_type=definition.account_type.value,
                normal_side=definition.normal_side.value,
                currency=cur,
                debit_total=dr,
                credit_total=cr,
                balance=sign * (dr - cr),
                functional_balance=sign * (fdr - fcr),
            )
        )
        by_currency[cur]["debits"] += dr
        by_currency[cur]["credits"] += cr
        functional["debits"] += fdr
        functional["credits"] += fcr
    balanced = functional["debits"] == functional["credits"] and all(
        v["debits"] == v["credits"] for v in by_currency.values()
    )
    return TrialBalance(
        rows=out, by_currency=dict(by_currency), functional=functional, balanced=balanced
    )


# ------------------------------------------------------------------ reconciliation
def reconciliation_runs(
    session: Session, account: PortfolioAccount, limit: int = 20
) -> list[RunSummaryView]:
    sequence = last_sequence(session, account.id)
    runs = session.scalars(
        select(ReconciliationRunRecord)
        .where(ReconciliationRunRecord.portfolio_account_id == account.id)
        .order_by(ReconciliationRunRecord.finished_at.desc())
        .limit(limit)
    ).all()
    return [_run_view(r, sequence) for r in runs]


def reconciliation_run(session: Session, run_id: uuid.UUID) -> dict[str, Any]:
    run = session.get(ReconciliationRunRecord, run_id)
    if run is None:
        raise NotFoundError(f"reconciliation run {run_id} does not exist")
    results = session.scalars(
        select(ReconciliationResultRecord)
        .where(ReconciliationResultRecord.run_id == run_id)
        .order_by(ReconciliationResultRecord.id)
    ).all()
    sequence = last_sequence(session, run.portfolio_account_id)
    return {
        "run": _run_view(run, sequence).model_dump(),
        "results": [_result_view(r).model_dump() for r in results],
    }


# ------------------------------------------------------------------ reference
def chart_of_accounts(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "code": r.code,
            "name": r.name,
            "account_type": r.account_type.value,
            "normal_side": r.normal_side.value,
            "category": r.category.value,
            "asset_rule": r.asset_rule.value,
            "allows_amount": r.allows_amount,
            "allows_functional_only": r.allows_functional_only,
            "description": r.description,
        }
        for r in session.scalars(select(LedgerAccountRecord).order_by(LedgerAccountRecord.code))
    ]


def posting_templates(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "code": t.code,
            "version": t.version,
            "entry_type": t.entry_type.value,
            "description": t.description,
            "rules": t.definition.get("rules", []),
            "definition_sha256": t.definition_sha256,
            "first_used_at": t.created_at,
        }
        for t in session.scalars(select(PostingTemplateRecord).order_by(PostingTemplateRecord.code))
    ]


def fx_rates(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "base_currency": r.base_currency,
            "quote_currency": r.quote_currency,
            "rate": r.rate,
            "observed_at": r.observed_at,
            "source": r.source.value,
            "provider": r.provider,
            "provenance": r.provenance,
            "recorded_at": r.recorded_at,
        }
        for r in session.scalars(
            select(FxRateRecord).order_by(FxRateRecord.observed_at.desc()).limit(limit)
        )
    ]


def snapshots(session: Session, account: PortfolioAccount, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(PortfolioSnapshotRecord)
        .where(PortfolioSnapshotRecord.portfolio_account_id == account.id)
        .order_by(PortfolioSnapshotRecord.as_of.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": s.id,
            "as_of": s.as_of,
            "valuation_status": s.valuation_status.value,
            "entry_sequence": s.entry_sequence,
            "valuation_entry_id": s.valuation_entry_id,
            "equity": s.equity,
            "contributed_capital": s.contributed_capital,
            "investment_return": s.investment_return,
            "cash": s.cash,
            "long_value": s.long_value,
            "short_liability": s.short_liability,
            "realized_pnl": s.realized_pnl,
            "unrealized_pnl": s.unrealized_pnl,
            "issues": s.issues,
        }
        for s in rows
    ]


__all__ = [
    "DISCLAIMER",
    "NOT_INITIALIZED",
    "AccountView",
    "BalanceRow",
    "EntryDetail",
    "EntrySummary",
    "HoldingView",
    "Overview",
    "TrialBalance",
    "accounts",
    "chart_of_accounts",
    "entry",
    "fx_rates",
    "holdings",
    "journal",
    "overview",
    "pnl",
    "posting_templates",
    "reconciliation_run",
    "reconciliation_runs",
    "resolve_account",
    "snapshots",
    "trial_balance",
]

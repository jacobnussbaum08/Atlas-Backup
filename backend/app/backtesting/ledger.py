"""The backtest's books: Phase 6A posting templates on an in-memory ledger.

A run has its own simulated paper account that exists only in memory. Every financial
event is posted with the **same** 6A templates (``app.accounting.templates.TEMPLATES``),
validated with the same balancing rules (R1, R2, line rules: ``ledger.validate``) and the
same post-state invariants (``state.invariant_violations``) the posting service applies
before it writes; only the storage differs. The journal is kept and stored with the run.

:meth:`SimulatedLedger.reconcile` is independent of the running state: it replays the
journal into a fresh ledger and compares every balance and position, checks the
accounting equation (equity from balances == equity from its components), the quantity
ledger against the cost/proceeds accounts, and cash conservation (opening funding plus all
cash movements). A run whose books do not reconcile fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.accounting import chart
from app.accounting.enums import EntryType
from app.accounting.ledger import EntryDraft, EntryValidationError, validate
from app.accounting.money import ZERO
from app.accounting.state import InsufficientBalanceError, LedgerState, invariant_violations
from app.accounting.templates import (
    TEMPLATES,
    InstrumentInfo,
    PriceMark,
    RateQuote,
    TemplateError,
    ValuationParams,
    _Builder,
)
from app.portfolio.figures import PortfolioFigures, figures


class LedgerError(RuntimeError):
    """A posting was refused; in a backtest this stops the run (nothing is skipped)."""


class CashDividendParams(BaseModel):
    """D-237: the cash dividend of one position at an ex-date (backtest ledger only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: uuid.UUID
    currency: str
    direction: Literal["LONG", "SHORT"]
    ex_date: date
    rate: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)


def cash_dividend(state: LedgerState, ctx: Any, p: CashDividendParams) -> EntryDraft:
    """A long position receives the dividend (Dr 1000 Cash / Cr 4000 realized, with the
    asset); a short position pays it to the lender (Dr 4000 / Cr 1000). The RAW close
    falls by the dividend on the ex-date, so the position's total return counts it once.
    Recorded in realized P/L per asset (the chart has no dividend account; the backtest
    ledger only)."""
    # The 6A entry types are exactly the 6A templates' (the live journal admits only those),
    # so the draft carries a placeholder; the backtest journal records BacktestEntryType.
    b = _Builder(
        state,
        ctx,
        EntryType.FINANCING_COST,
        f"Cash dividend {p.amount} {p.currency} ({p.direction})",
    )
    if p.direction == "LONG":
        b.pair(
            chart.CASH,
            chart.REALIZED_PNL,
            p.currency,
            p.amount,
            b.spot(p.currency, p.amount),
            credit_asset=p.asset_id,
            memo="cash dividend received (ex-date)",
        )
    else:
        b.pay_cash(
            chart.REALIZED_PNL,
            p.currency,
            p.amount,
            p.asset_id,
            "dividend paid on a short (ex-date)",
        )
    return b.done(ex_date=p.ex_date.isoformat(), rate=str(p.rate), quantity=str(p.quantity))


class BacktestEntryType(StrEnum):
    """Journal entry types of the backtest ledger's own templates (never posted live)."""

    DIVIDEND = "DIVIDEND"


@dataclass(frozen=True)
class BacktestTemplateDef:
    code: str
    version: int
    journal_entry_type: BacktestEntryType
    params: type[BaseModel]
    build: Callable[..., EntryDraft]
    description: str
    rules: tuple[str, ...] = ()


BACKTEST_TEMPLATES: dict[str, BacktestTemplateDef] = {
    "cash_dividend": BacktestTemplateDef(
        "cash_dividend",
        1,
        BacktestEntryType.DIVIDEND,
        CashDividendParams,
        cash_dividend,
        "Cash dividend of a backtest position at its ex-date (D-237; backtest ledger only)",
        (
            "long: Dr 1000 Cash / Cr 4000 Realized (asset); short: Dr 4000 (asset) / Cr 1000",
            "amount = rate x quantity held over the previous close, rounded to minor units",
        ),
    )
}


class BacktestTemplateContext:
    """The 6A TemplateContext of a backtest: the functional currency and the instrument
    specifications resolved for the run. Foreign currencies have no rates (USD only)."""

    def __init__(self, functional: str, instruments: dict[uuid.UUID, InstrumentInfo]):
        self._functional = functional
        self.instruments = instruments

    @property
    def functional_currency(self) -> str:
        return self._functional

    def spot_rate(self, currency: str) -> RateQuote:
        raise TemplateError(f"no {currency}/{self._functional} rate in a backtest (USD only)")

    def instrument(self, asset_id: uuid.UUID) -> InstrumentInfo:
        found = self.instruments.get(asset_id)
        if found is None:
            raise TemplateError(f"no instrument specification for {asset_id}")
        return found


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    effective_at: datetime
    template: str
    entry_type: EntryType | BacktestEntryType
    description: str
    params: dict[str, Any]
    lines: list[dict[str, Any]]
    movements: list[dict[str, Any]]
    source_ref: str
    draft: EntryDraft = field(repr=False, compare=False)

    def as_json(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "effective_at": self.effective_at.isoformat(),
            "template": self.template,
            "entry_type": self.entry_type.value,
            "description": self.description,
            "params": self.params,
            "lines": self.lines,
            "movements": self.movements,
            "source_ref": self.source_ref,
        }


def _line(line: Any) -> dict[str, Any]:
    return {
        "account": line.account_code,
        "side": line.side.value,
        "currency": line.currency,
        "amount": str(line.amount),
        "functional": str(line.functional_amount),
        "asset_id": None if line.asset_id is None else str(line.asset_id),
    }


def _movement(m: Any) -> dict[str, Any]:
    return {
        "asset_id": str(m.asset_id),
        "type": m.movement_type.value,
        "quantity_delta": str(m.quantity_delta),
        "quantity_after": str(m.quantity_after),
        "cost_after": str(m.cost_after),
        "price": None if m.price is None else str(m.price),
        "realized_pnl": str(m.realized_pnl),
    }


@dataclass
class ReconciliationResult:
    passed: bool
    checks: list[dict[str, Any]]
    sequence: int

    def failures(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"]]


class SimulatedLedger:
    def __init__(self, functional: str, ctx: BacktestTemplateContext):
        self.functional = functional
        self.ctx = ctx
        self.state = LedgerState(functional)
        self.journal: list[JournalEntry] = []

    @property
    def sequence(self) -> int:
        return len(self.journal)

    def post(
        self,
        template: str,
        params: dict[str, Any] | BaseModel,
        *,
        at: datetime,
        source_ref: str,
        skip_if_empty: bool = False,
    ) -> JournalEntry | None:
        definition = TEMPLATES.get(template) or BACKTEST_TEMPLATES.get(template)
        if definition is None:
            raise LedgerError(f"unknown posting template {template!r}")
        raw = params.model_dump(mode="json") if isinstance(params, BaseModel) else params
        try:
            parsed = definition.params.model_validate(
                params.model_dump() if isinstance(params, BaseModel) else params
            )
            draft = definition.build(self.state, self.ctx, parsed)
            if skip_if_empty and not draft.lines and not draft.movements:
                return None
            validate(draft, self.functional)
        except (TemplateError, EntryValidationError, InsufficientBalanceError, ValueError) as exc:
            raise LedgerError(f"{template} refused at {at.isoformat()}: {exc}") from None
        after = self.state.copy()
        for line in draft.lines:
            after.apply_line(line)
        for movement in draft.movements:
            after.apply_movement(movement)
        before = set(invariant_violations(self.state))
        new = [p for p in invariant_violations(after) if p not in before]
        if new:
            raise LedgerError(f"{template} would leave the books inconsistent: " + "; ".join(new))
        self.state = after
        entry = JournalEntry(
            sequence=len(self.journal) + 1,
            effective_at=at,
            template=template,
            entry_type=definition.journal_entry_type
            if isinstance(definition, BacktestTemplateDef)
            else draft.entry_type,
            description=draft.description,
            params=_jsonable(raw),
            lines=[_line(x) for x in draft.lines],
            movements=[_movement(m) for m in draft.movements],
            source_ref=source_ref,
            draft=draft,
        )
        self.journal.append(entry)
        return entry

    def value(self, at: datetime, marks: list[PriceMark]) -> JournalEntry | None:
        """A valuation checkpoint (6A ``valuation``): adjustments to the given marks."""
        if not marks:
            return None
        return self.post(
            "valuation",
            ValuationParams(as_of=at, marks=tuple(marks)),
            at=at,
            source_ref="valuation",
            skip_if_empty=True,
        )

    def marked(self, marks: list[PriceMark], at: datetime) -> LedgerState:
        """A copy of the state marked to ``marks`` in memory (nothing posted), as 6B does."""
        if not marks:
            return self.state.copy()
        definition = TEMPLATES["valuation"]
        draft = definition.build(
            self.state, self.ctx, ValuationParams(as_of=at, marks=tuple(marks))
        )
        out = self.state.copy()
        for line in draft.lines:
            out.apply_line(line)
        return out

    def figures(self) -> PortfolioFigures:
        return figures(self.state)

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self) -> ReconciliationResult:
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": passed, "detail": detail})

        replay = LedgerState(self.functional)
        cash_moves = ZERO
        for entry in self.journal:
            draft = entry.draft
            try:
                validate(draft, self.functional)
            except EntryValidationError as exc:
                check("entry_balanced", False, f"entry {entry.sequence}: {exc}")
            for line in draft.lines:
                replay.apply_line(line)
                if line.account_code == chart.CASH:
                    cash_moves += line.signed_amount
            for movement in draft.movements:
                replay.apply_movement(movement)
        keys = set(replay.balances) | set(self.state.balances)
        mismatched = [
            k
            for k in keys
            if replay.balance(*k).amount != self.state.balance(*k).amount
            or replay.balance(*k).functional != self.state.balance(*k).functional
        ]
        check(
            "balances_replay",
            not mismatched,
            "every balance equals the journal replay"
            if not mismatched
            else f"{len(mismatched)} balance(s) differ from the journal replay",
        )
        positions_differ = [
            a
            for a in set(replay.positions) | set(self.state.positions)
            if (replay.positions.get(a) and replay.positions[a].quantity)
            != (self.state.positions.get(a) and self.state.positions[a].quantity)
        ]
        check(
            "quantity_ledger",
            not positions_differ,
            "positions equal the quantity-ledger replay"
            if not positions_differ
            else f"{len(positions_differ)} position(s) differ",
        )
        problems = invariant_violations(self.state)
        check("invariants", not problems, "; ".join(problems) or "no invariant violated")
        totals = figures(self.state)
        check(
            "accounting_equation",
            totals.equity == totals.equity_by_components,
            f"equity {totals.equity} vs components {totals.equity_by_components}",
        )
        cash = self.state.balance(chart.CASH, self.functional).amount
        check(
            "cash_conservation",
            cash == cash_moves,
            f"cash {cash} vs the sum of journal cash movements {cash_moves}",
        )
        check(
            "cash_non_negative",
            cash >= ZERO,
            f"cash {cash}",
        )
        return ReconciliationResult(
            passed=all(c["passed"] for c in checks), checks=checks, sequence=self.sequence
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal | uuid.UUID | datetime):
        return str(value)
    return value


__all__ = [
    "BacktestTemplateContext",
    "JournalEntry",
    "LedgerError",
    "ReconciliationResult",
    "SimulatedLedger",
]

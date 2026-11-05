"""Automatic accounting reconciliation (spec 16.6; decision D-135).

A run reads the account's books under a share lock (postings wait; nothing changes while
it reads), runs every check below, and stores the run and one result per check and scope.
It never corrects anything: a discrepancy is recorded as a FAIL result with severity HIGH,
logged at error level (``accounting.reconciliation_failed``) and written to the audit log,
so it surfaces as a high-priority accounting error on the dashboard and in the API.

Checks (``check_code``):

ENTRY_BALANCE        every entry has >= 2 lines, balances per currency (R1) and in
                     functional value (R2)
TRIAL_BALANCE        the whole ledger balances per currency and in functional value
SEQUENCE             entry numbers are 1..n without gaps; lines carry their entry's number
CURRENCY_BALANCES    the cached ``currency_balances`` equal balances recomputed from lines
CASH                 unrestricted and restricted cash are non-negative in every currency
ASSET_QUANTITIES     each asset's quantity-ledger running totals follow from its movements
POSITIONS            quantity-ledger cost basis equals the ledger's cost (1100) or proceeds
                     (2100) per asset, and the ledger state satisfies every invariant
SHORT_LIABILITIES    restricted proceeds (1010) equal the short liability (2100) per asset
REALIZED_PNL         4000 equals the realized P/L recorded on quantity movements
UNREALIZED_VALUATION 4100 equals the valuation adjustments 1150 + 2150 (no orphan value)
FX_CLEARING          the FX clearing account's functional value is zero
FX_TRANSLATION       1910 equals the unrealized FX recorded in 4300, per currency
EQUITY               assets - liabilities equals capital + income - expenses
SNAPSHOT             the latest snapshot's figures and positions equal the ledger as of its
                     sequence; positions it marked carry quantity x price (POSITION_VALUATION)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting import chart
from app.accounting.enums import CheckStatus, ReconciliationStatus, Severity, Side
from app.accounting.money import ZERO, currency, mul
from app.accounting.posting import last_sequence, load_state
from app.accounting.reference import instrument_info
from app.accounting.state import LedgerState, invariant_violations
from app.accounting.templates import TemplateError
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models import ActorType, PortfolioAccount
from app.database.models.accounting import (
    AssetQuantityRecord,
    CurrencyBalanceRecord,
    JournalEntryRecord,
    JournalLineRecord,
    ReconciliationResultRecord,
    ReconciliationRunRecord,
)
from app.database.models.portfolio import (
    SNAPSHOT_FIGURES,
    PortfolioSnapshotPositionRecord,
    PortfolioSnapshotRecord,
)
from app.database.repositories.audit_log import AuditLogRepository
from app.database.sessions import session_scope
from app.portfolio.figures import figures

logger = get_logger(__name__)


@dataclass(frozen=True)
class Check:
    code: str
    scope: str
    ok: bool
    message: str
    currency: str | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None


@dataclass(frozen=True)
class RunSummary:
    run_id: uuid.UUID
    status: ReconciliationStatus
    checks_total: int
    checks_failed: int
    entry_sequence: int
    failures: list[Check]


_DEBIT = JournalLineRecord.side == Side.DEBIT
_SIGNED_AMOUNT = case((_DEBIT, JournalLineRecord.amount), else_=-JournalLineRecord.amount)
_SIGNED_FUNCTIONAL = case(
    (_DEBIT, JournalLineRecord.functional_amount), else_=-JournalLineRecord.functional_amount
)


def _compare(
    code: str, scope: str, expected: Decimal, actual: Decimal, what: str, cur: str | None = None
) -> Check:
    ok = expected == actual
    message = f"{what}: {actual}" if ok else f"{what}: expected {expected}, found {actual}"
    return Check(code, scope, ok, message, cur, expected, actual)


class ReconciliationService:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def run(self, account_id: uuid.UUID, *, actor: str, trigger: str = "MANUAL") -> RunSummary:
        with session_scope(self._session_factory) as session:
            return self.run_in(session, account_id, actor=actor, trigger=trigger)

    def run_in(
        self, session: Session, account_id: uuid.UUID, *, actor: str, trigger: str
    ) -> RunSummary:
        started = self._clock.now()
        account = session.scalar(
            select(PortfolioAccount)
            .where(PortfolioAccount.id == account_id)
            .with_for_update(read=True)
        )
        if account is None:
            raise ValueError(f"portfolio account {account_id} does not exist")
        sequence = last_sequence(session, account_id)
        state = load_state(session, account_id, account.base_currency)
        checks: list[Check] = []
        for check in (
            self._entry_balance,
            self._trial_balance,
            self._sequence,
            self._currency_balances,
        ):
            checks.extend(check(session, account_id))
        checks.extend(self._cash(state))
        checks.extend(self._quantities(session, account_id))
        checks.extend(self._positions(state))
        checks.extend(self._shorts(state))
        checks.extend(self._realized(session, account_id, state))
        checks.extend(self._unrealized(state))
        checks.extend(self._fx(state))
        checks.extend(self._equity(state))
        checks.extend(self._snapshot(session, account_id, account.base_currency))

        failures = [c for c in checks if not c.ok]
        status = ReconciliationStatus.FAILED if failures else ReconciliationStatus.PASSED
        run = ReconciliationRunRecord(
            portfolio_account_id=account_id,
            entry_sequence=sequence,
            status=status,
            checks_total=len(checks),
            checks_failed=len(failures),
            trigger=trigger,
            created_by=actor,
            started_at=started,
            finished_at=self._clock.now(),
        )
        session.add(run)
        session.flush()
        session.add_all(
            ReconciliationResultRecord(
                run_id=run.id,
                check_code=c.code,
                scope=c.scope[:200],
                currency=c.currency,
                expected=c.expected,
                actual=c.actual,
                difference=(c.actual - c.expected)
                if c.actual is not None and c.expected is not None
                else None,
                status=CheckStatus.PASS if c.ok else CheckStatus.FAIL,
                severity=Severity.INFO if c.ok else Severity.HIGH,
                message=c.message,
            )
            for c in checks
        )
        if failures:
            logger.error(
                "accounting.reconciliation_failed",
                account_id=str(account_id),
                run_id=str(run.id),
                failed=len(failures),
                checks=sorted({c.code for c in failures}),
            )
            AuditLogRepository(session).append(
                actor_type=ActorType.SUBSYSTEM,
                actor_id=actor,
                action="accounting.reconciliation_failed",
                entity_type="reconciliation_run",
                entity_id=str(run.id),
                reason="unexplained accounting discrepancy (high priority)",
                details={
                    "account_id": str(account_id),
                    "failed": [f"{c.code} {c.scope}" for c in failures][:50],
                },
            )
        else:
            logger.info(
                "accounting.reconciliation_passed",
                account_id=str(account_id),
                checks=len(checks),
                sequence=sequence,
            )
        session.flush()
        return RunSummary(run.id, status, len(checks), len(failures), sequence, failures)

    # -------------------------------------------------------------- SQL checks
    def _entry_balance(self, session: Session, account_id: uuid.UUID) -> list[Check]:
        out: list[Check] = []
        short = session.execute(
            select(JournalEntryRecord.sequence, func.count(JournalLineRecord.id))
            .outerjoin(JournalLineRecord, JournalLineRecord.entry_id == JournalEntryRecord.id)
            .where(JournalEntryRecord.portfolio_account_id == account_id)
            .group_by(JournalEntryRecord.sequence)
            .having(func.count(JournalLineRecord.id) < 2)
        ).all()
        for seq, count in short:
            out.append(Check("ENTRY_BALANCE", f"entry #{seq}", False, f"{count} line(s)"))
        per_currency = session.execute(
            select(
                JournalLineRecord.sequence,
                JournalLineRecord.currency,
                func.sum(_SIGNED_AMOUNT),
            )
            .where(JournalLineRecord.portfolio_account_id == account_id)
            .group_by(JournalLineRecord.sequence, JournalLineRecord.currency)
            .having(func.sum(_SIGNED_AMOUNT) != 0)
        ).all()
        for seq, cur, total in per_currency:
            out.append(
                Check(
                    "ENTRY_BALANCE",
                    f"entry #{seq}",
                    False,
                    f"R1: {cur} differs by {total}",
                    cur,
                    ZERO,
                    Decimal(total),
                )
            )
        functional = session.execute(
            select(
                JournalLineRecord.sequence,
                func.sum(_SIGNED_FUNCTIONAL),
            )
            .where(JournalLineRecord.portfolio_account_id == account_id)
            .group_by(JournalLineRecord.sequence)
            .having(func.sum(_SIGNED_FUNCTIONAL) != 0)
        ).all()
        for seq, total in functional:
            out.append(
                Check(
                    "ENTRY_BALANCE",
                    f"entry #{seq}",
                    False,
                    f"R2: differs by {total}",
                    None,
                    ZERO,
                    Decimal(total),
                )
            )
        if not out:
            entries = session.scalar(
                select(func.count())
                .select_from(JournalEntryRecord)
                .where(JournalEntryRecord.portfolio_account_id == account_id)
            )
            out.append(
                Check(
                    "ENTRY_BALANCE",
                    "all entries",
                    True,
                    f"{entries} entries balance per currency and in functional value",
                )
            )
        return out

    def _trial_balance(self, session: Session, account_id: uuid.UUID) -> list[Check]:
        rows = session.execute(
            select(
                JournalLineRecord.currency,
                func.sum(_SIGNED_AMOUNT),
                func.sum(_SIGNED_FUNCTIONAL),
            )
            .where(JournalLineRecord.portfolio_account_id == account_id)
            .group_by(JournalLineRecord.currency)
        ).all()
        out = [
            _compare(
                "TRIAL_BALANCE",
                f"currency {cur}",
                ZERO,
                Decimal(amount),
                "debits minus credits",
                cur,
            )
            for cur, amount, _ in rows
        ]
        functional = sum((Decimal(f) for _, _, f in rows), ZERO)
        out.append(
            _compare(
                "TRIAL_BALANCE", "functional", ZERO, functional, "functional debits minus credits"
            )
        )
        return out

    def _sequence(self, session: Session, account_id: uuid.UUID) -> list[Check]:
        count, low, high = session.execute(
            select(
                func.count(),
                func.min(JournalEntryRecord.sequence),
                func.max(JournalEntryRecord.sequence),
            ).where(JournalEntryRecord.portfolio_account_id == account_id)
        ).one()
        gapless = count == 0 or (low == 1 and high == count)
        mismatched = session.scalar(
            select(func.count())
            .select_from(JournalLineRecord)
            .join(JournalEntryRecord, JournalEntryRecord.id == JournalLineRecord.entry_id)
            .where(
                JournalLineRecord.portfolio_account_id == account_id,
                (JournalLineRecord.sequence != JournalEntryRecord.sequence)
                | (
                    JournalLineRecord.portfolio_account_id
                    != JournalEntryRecord.portfolio_account_id
                ),
            )
        )
        return [
            Check(
                "SEQUENCE",
                "entries",
                gapless,
                f"{count} entries numbered {low or 0}..{high or 0}"
                + ("" if gapless else " (gap or duplicate)"),
            ),
            _compare(
                "SEQUENCE",
                "lines",
                ZERO,
                Decimal(mismatched or 0),
                "lines whose number differs from their entry's",
            ),
        ]

    def _currency_balances(self, session: Session, account_id: uuid.UUID) -> list[Check]:
        recomputed = {
            (code, cur): (Decimal(a), Decimal(f), int(n), int(s))
            for code, cur, a, f, n, s in session.execute(
                select(
                    JournalLineRecord.account_code,
                    JournalLineRecord.currency,
                    func.sum(_SIGNED_AMOUNT),
                    func.sum(_SIGNED_FUNCTIONAL),
                    func.count(),
                    func.max(JournalLineRecord.sequence),
                )
                .where(JournalLineRecord.portfolio_account_id == account_id)
                .group_by(JournalLineRecord.account_code, JournalLineRecord.currency)
            )
        }
        cached = {
            (r.account_code, r.currency): (
                r.balance,
                r.functional_balance,
                r.line_count,
                r.last_sequence,
            )
            for r in session.scalars(
                select(CurrencyBalanceRecord).where(
                    CurrencyBalanceRecord.portfolio_account_id == account_id
                )
            )
        }
        out: list[Check] = []
        for key in sorted(set(recomputed) | set(cached)):
            code, cur = key
            want = recomputed.get(key)
            have = cached.get(key)
            scope = f"{code} {cur}"
            if want is None or have is None:
                out.append(
                    Check(
                        "CURRENCY_BALANCES",
                        scope,
                        False,
                        "cached balance without lines"
                        if want is None
                        else "lines without a cached balance",
                        cur,
                    )
                )
                continue
            out.append(_compare("CURRENCY_BALANCES", scope, want[0], have[0], "balance", cur))
            out.append(
                _compare(
                    "CURRENCY_BALANCES",
                    scope + " functional",
                    want[1],
                    have[1],
                    "functional balance",
                    cur,
                )
            )
            if (want[2], want[3]) != (have[2], have[3]):
                out.append(
                    Check(
                        "CURRENCY_BALANCES",
                        scope + " counts",
                        False,
                        f"lines/sequence expected {want[2]}/{want[3]}, cached {have[2]}/{have[3]}",
                        cur,
                    )
                )
        if not out:
            out.append(Check("CURRENCY_BALANCES", "all", True, "no balances yet"))
        return out

    # -------------------------------------------------------------- state checks
    @staticmethod
    def _cash(state: LedgerState) -> list[Check]:
        out: list[Check] = []
        for cur in sorted(state.currencies()):
            free = sum(
                (
                    b.amount
                    for (c, k, _), b in state.balances.items()
                    if c == chart.CASH and k == cur
                ),
                ZERO,
            )
            restricted = sum(
                (
                    b.amount
                    for (c, k, _), b in state.balances.items()
                    if c == chart.RESTRICTED_CASH and k == cur
                ),
                ZERO,
            )
            if free == ZERO and restricted == ZERO:
                continue
            out.append(
                Check(
                    "CASH",
                    f"{cur} unrestricted",
                    free >= ZERO,
                    f"unrestricted cash {free} {cur}",
                    cur,
                    None,
                    free,
                )
            )
            out.append(
                Check(
                    "CASH",
                    f"{cur} restricted",
                    restricted >= ZERO,
                    f"restricted short proceeds {restricted} {cur}",
                    cur,
                    None,
                    restricted,
                )
            )
        return out or [Check("CASH", "all", True, "no cash balances")]

    def _quantities(self, session: Session, account_id: uuid.UUID) -> list[Check]:
        rows = session.scalars(
            select(AssetQuantityRecord)
            .where(AssetQuantityRecord.portfolio_account_id == account_id)
            .order_by(AssetQuantityRecord.asset_id, AssetQuantityRecord.sequence)
        ).all()
        by_asset: dict[uuid.UUID, list[AssetQuantityRecord]] = defaultdict(list)
        for row in rows:
            by_asset[row.asset_id].append(row)
        out: list[Check] = []
        for asset_id, movements in by_asset.items():
            qty = cost = fcost = ZERO
            broken: list[str] = []
            for m in movements:
                qty += m.quantity_delta
                cost += m.cost_delta
                fcost += m.functional_cost_delta
                if (qty, cost, fcost) != (m.quantity_after, m.cost_after, m.functional_cost_after):
                    broken.append(f"#{m.sequence}")
            out.append(
                Check(
                    "ASSET_QUANTITIES",
                    f"asset {asset_id}",
                    not broken,
                    f"{len(movements)} movements, quantity {qty}"
                    + (f"; running totals break at {', '.join(broken)}" if broken else ""),
                    movements[0].currency,
                    None,
                    qty,
                )
            )
        return out or [Check("ASSET_QUANTITIES", "all", True, "no quantity movements")]

    @staticmethod
    def _positions(state: LedgerState) -> list[Check]:
        problems = invariant_violations(state)
        out = [
            Check(
                "POSITIONS",
                "ledger invariants",
                not problems,
                "; ".join(problems) if problems else "every invariant holds",
            )
        ]
        for asset_id, pos in state.positions.items():
            code = chart.LONG_COST if pos.quantity > ZERO else chart.SHORT_PROCEEDS
            held, functional = state.normal_balance(code, pos.currency, asset_id)
            if pos.quantity == ZERO:
                held, functional = ZERO, ZERO
            out.append(
                _compare(
                    "POSITIONS",
                    f"asset {asset_id} cost basis",
                    pos.cost,
                    held,
                    f"{code} carrying amount",
                    pos.currency,
                )
            )
            out.append(
                _compare(
                    "POSITIONS",
                    f"asset {asset_id} functional cost",
                    pos.functional_cost,
                    functional,
                    f"{code} functional value",
                    pos.currency,
                )
            )
        return out

    @staticmethod
    def _shorts(state: LedgerState) -> list[Check]:
        out: list[Check] = []
        assets = {
            a for (c, _, a) in state.balances if c in (chart.RESTRICTED_CASH, chart.SHORT_PROCEEDS)
        }
        for asset_id in sorted(assets, key=str):
            for cur in sorted(state.currencies()):
                collateral = state.normal_balance(chart.RESTRICTED_CASH, cur, asset_id)
                liability = state.normal_balance(chart.SHORT_PROCEEDS, cur, asset_id)
                if collateral == (ZERO, ZERO) and liability == (ZERO, ZERO):
                    continue
                out.append(
                    _compare(
                        "SHORT_LIABILITIES",
                        f"asset {asset_id}",
                        liability[0],
                        collateral[0],
                        "restricted proceeds vs liability",
                        cur,
                    )
                )
                out.append(
                    _compare(
                        "SHORT_LIABILITIES",
                        f"asset {asset_id} functional",
                        liability[1],
                        collateral[1],
                        "restricted proceeds vs liability (functional)",
                        cur,
                    )
                )
        return out or [Check("SHORT_LIABILITIES", "all", True, "no short positions")]

    def _realized(self, session: Session, account_id: uuid.UUID, state: LedgerState) -> list[Check]:
        recorded = {
            cur: (Decimal(a), Decimal(f))
            for cur, a, f in session.execute(
                select(
                    AssetQuantityRecord.currency,
                    func.sum(AssetQuantityRecord.realized_pnl),
                    func.sum(AssetQuantityRecord.realized_pnl_functional),
                )
                .where(AssetQuantityRecord.portfolio_account_id == account_id)
                .group_by(AssetQuantityRecord.currency)
            )
        }
        out: list[Check] = []
        currencies = set(recorded) | {k for (c, k, _) in state.balances if c == chart.REALIZED_PNL}
        for cur in sorted(currencies):
            ledger_amount = -sum(
                (
                    b.amount
                    for (c, k, _), b in state.balances.items()
                    if c == chart.REALIZED_PNL and k == cur
                ),
                ZERO,
            )
            ledger_functional = -sum(
                (
                    b.functional
                    for (c, k, _), b in state.balances.items()
                    if c == chart.REALIZED_PNL and k == cur
                ),
                ZERO,
            )
            want = recorded.get(cur, (ZERO, ZERO))
            out.append(
                _compare(
                    "REALIZED_PNL",
                    cur,
                    want[0],
                    ledger_amount,
                    "4000 vs quantity-ledger realized P/L",
                    cur,
                )
            )
            out.append(
                _compare(
                    "REALIZED_PNL",
                    f"{cur} functional",
                    want[1],
                    ledger_functional,
                    "4000 vs quantity-ledger realized P/L (functional)",
                    cur,
                )
            )
        return out or [Check("REALIZED_PNL", "all", True, "nothing realized")]

    @staticmethod
    def _unrealized(state: LedgerState) -> list[Check]:
        out: list[Check] = []
        for cur in sorted(state.currencies()):

            def total(code: str, field: str, cur: str = cur) -> Decimal:
                return sum(
                    (
                        getattr(b, field)
                        for (c, k, _), b in state.balances.items()
                        if c == code and k == cur
                    ),
                    ZERO,
                )

            pnl = (
                -total(chart.UNREALIZED_PNL, "amount"),
                -total(chart.UNREALIZED_PNL, "functional"),
            )
            adj = (
                total(chart.LONG_VALUATION, "amount") + total(chart.SHORT_VALUATION, "amount"),
                total(chart.LONG_VALUATION, "functional")
                + total(chart.SHORT_VALUATION, "functional"),
            )
            if pnl == (ZERO, ZERO) and adj == (ZERO, ZERO):
                continue
            out.append(
                _compare(
                    "UNREALIZED_VALUATION",
                    cur,
                    adj[0],
                    pnl[0],
                    "4100 vs valuation adjustments 1150 + 2150",
                    cur,
                )
            )
            out.append(
                _compare(
                    "UNREALIZED_VALUATION",
                    f"{cur} functional",
                    adj[1],
                    pnl[1],
                    "4100 vs valuation adjustments (functional)",
                    cur,
                )
            )
        return out or [Check("UNREALIZED_VALUATION", "all", True, "no open valuation adjustments")]

    @staticmethod
    def _fx(state: LedgerState) -> list[Check]:
        clearing = sum(
            (b.functional for (c, _, _), b in state.balances.items() if c == chart.FX_CLEARING),
            ZERO,
        )
        out = [
            _compare("FX_CLEARING", "functional", ZERO, clearing, "FX clearing functional value")
        ]
        for cur in sorted(state.currencies()):
            if cur == state.functional_currency:
                continue
            translation = state.translation(cur)
            recognized = -state.balance(chart.UNREALIZED_FX, cur).functional
            out.append(
                _compare(
                    "FX_TRANSLATION",
                    cur,
                    recognized,
                    translation,
                    "1910 translation vs 4300 unrealized FX",
                    cur,
                )
            )
        return out

    @staticmethod
    def _equity(state: LedgerState) -> list[Check]:
        totals = figures(state)
        return [
            _compare(
                "EQUITY",
                "accounting equation",
                totals.equity_by_components,
                totals.equity,
                "assets - liabilities vs capital + income - expenses",
                state.functional_currency,
            ),
            _compare(
                "EQUITY",
                "investment return",
                totals.equity - totals.contributed_capital,
                totals.investment_return,
                "equity - contributed capital",
                state.functional_currency,
            ),
        ]

    def _snapshot(self, session: Session, account_id: uuid.UUID, functional: str) -> list[Check]:
        snap = session.scalar(
            select(PortfolioSnapshotRecord)
            .where(PortfolioSnapshotRecord.portfolio_account_id == account_id)
            .order_by(PortfolioSnapshotRecord.as_of.desc())
            .limit(1)
        )
        if snap is None:
            return [Check("SNAPSHOT", "latest", True, "no snapshot yet")]
        state = load_state(session, account_id, functional, up_to=snap.entry_sequence)
        totals = figures(state)
        out = [
            _compare(
                "SNAPSHOT",
                f"{snap.as_of.isoformat()} {name}",
                getattr(totals, name),
                getattr(snap, name),
                name,
                functional,
            )
            for name in SNAPSHOT_FIGURES
        ]
        positions = session.scalars(
            select(PortfolioSnapshotPositionRecord).where(
                PortfolioSnapshotPositionRecord.snapshot_id == snap.id
            )
        ).all()
        open_assets = {a for a, p in state.positions.items() if p.quantity != ZERO}
        out.append(
            Check(
                "SNAPSHOT",
                "positions",
                {p.asset_id for p in positions} == open_assets,
                f"{len(positions)} snapshot positions, {len(open_assets)} open",
            )
        )
        for p in positions:
            pos = state.positions.get(p.asset_id)
            if pos is None:
                continue
            out.append(
                _compare(
                    "SNAPSHOT",
                    f"asset {p.asset_id} quantity",
                    pos.quantity,
                    p.quantity,
                    "quantity",
                    p.currency,
                )
            )
            if p.marked and p.price is not None:
                try:
                    multiplier = instrument_info(session, p.asset_id, snap.as_of).multiplier
                except TemplateError as exc:
                    out.append(Check("POSITION_VALUATION", f"asset {p.asset_id}", False, str(exc)))
                    continue
                value = currency(p.currency).round(mul(abs(pos.quantity), p.price, multiplier))
                out.append(
                    _compare(
                        "POSITION_VALUATION",
                        f"asset {p.asset_id}",
                        value,
                        p.carried_value,
                        "carried value vs quantity x price",
                        p.currency,
                    )
                )
        return out


__all__ = ["Check", "ReconciliationService", "RunSummary"]

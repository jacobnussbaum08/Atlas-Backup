"""Phase 6A double-entry ledger against a real PostgreSQL.

Every account, instrument, price and rate here is TEST FIXTURE material; trades are
controlled test transactions on CONTROLLED_TEST accounts. Nothing is executed anywhere.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.accounting import chart
from app.accounting.enums import (
    CheckStatus,
    EntrySource,
    FxRateSource,
    ReconciliationStatus,
    Severity,
    ValuationStatus,
)
from app.accounting.posting import (
    IdempotencyConflictError,
    PostingError,
    ensure_template,
    load_state,
)
from app.accounting.templates import TEMPLATES
from app.database.models import AuditLog
from app.database.models.accounting import (
    CurrencyBalanceRecord,
    FxRateRecord,
    JournalEntryRecord,
    JournalLineRecord,
    PostingTemplateRecord,
    ReconciliationResultRecord,
)
from app.database.models.portfolio import PortfolioSnapshotPositionRecord, PortfolioSnapshotRecord
from app.database.sessions import session_scope
from app.portfolio.figures import figures
from app.tests.integration.accounting_support import NOW, Books, later, make_books

pytestmark = pytest.mark.integration


@pytest.fixture
def books(fresh_engine) -> Books:
    return make_books(fresh_engine)


def _figures(books: Books, account_id: uuid.UUID):
    with session_scope(books.factory) as session:
        return figures(load_state(session, account_id, "USD"))


def _reconciled(books: Books, account_id: uuid.UUID) -> None:
    summary = books.ctx.reconciliation.run(account_id, actor="test")
    assert summary.failures == []
    assert summary.status is ReconciliationStatus.PASSED


def _raw_entry(session, account_id, sequence, event_id, source=EntrySource.CONTROLLED_TEST):
    """A journal entry row written directly, bypassing the posting service."""
    template = ensure_template(session, TEMPLATES["fee"])
    entry = JournalEntryRecord(
        portfolio_account_id=account_id,
        sequence=sequence,
        event_id=event_id,
        request_sha256="0" * 64,
        entry_type="FEE",
        template_id=template.id,
        source=source,
        functional_currency="USD",
        effective_at=NOW,
        posted_at=NOW,
        description=event_id,
        payload={},
        created_by="test",
    )
    session.add(entry)
    return entry


# ------------------------------------------------------------------ opening funding
def test_opening_funding_persists_once_and_reconciles(books):
    primary = books.account("Atlas paper account", test=False)
    again = books.account("Atlas paper account", test=False)  # idempotent
    assert again == primary
    totals = _figures(books, primary)
    assert totals.cash == D("100000.00")
    assert totals.contributed_capital == D("100000.00")
    assert totals.equity == D("100000.00")
    assert totals.investment_return == 0
    with session_scope(books.factory) as session:
        entries = session.scalars(select(JournalEntryRecord)).all()
        assert [(e.sequence, e.entry_type.value, e.source.value) for e in entries] == [
            (1, "OPENING_FUNDING", "SYSTEM")
        ]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "accounting.entry_posted")
            )
            == 1
        )
    _reconciled(books, primary)


def test_controlled_test_transactions_never_reach_the_primary_account(books):
    primary = books.account("Atlas paper account", test=False)
    asset = books.asset("AAA")
    with pytest.raises(PostingError, match="never posted to the primary"):
        books.trade(primary, "long_purchase", "t-1", asset, "1", "10")
    with pytest.raises(PostingError, match="no production source"):
        books.post(
            primary,
            "long_purchase",
            "t-2",
            {"asset_id": str(asset), "quantity": "1", "price": "10"},
            source=EntrySource.SYSTEM,
        )
    test_account = books.account("Controlled test A")
    with pytest.raises(PostingError, match="posted by the system"):
        books.post(test_account, "opening_funding", "again", {"amount": "1"})
    # The database refuses it as well, whatever wrote the row.
    with books.factory() as session:
        _raw_entry(session, primary, 2, "raw")
        with pytest.raises(DBAPIError, match="PRIMARY"):
            session.flush()


# ------------------------------------------------------------------ immutability
def test_posted_records_are_immutable(books, fresh_engine):
    account = books.account("Controlled test A")
    books.post(account, "fee", "fee-1", {"kind": "FEE", "currency": "USD", "amount": "1.00"})
    books.trade(account, "long_purchase", "buy", books.asset("AAA"), "1", "10.00")
    statements = [
        "UPDATE journal_lines SET amount = amount + 1",
        "DELETE FROM journal_lines",
        "UPDATE journal_entries SET description = 'edited'",
        "DELETE FROM journal_entries",
        "TRUNCATE journal_lines CASCADE",
        "UPDATE ledger_accounts SET name = 'x'",
        "DELETE FROM currencies WHERE code = 'JPY'",
        "UPDATE asset_quantity_ledger SET quantity_after = 0",
        "UPDATE portfolio_accounts SET purpose = 'PRIMARY'",
        "UPDATE portfolio_accounts SET base_currency = 'EUR'",
        "DELETE FROM portfolio_accounts",
    ]
    for statement in statements:
        with fresh_engine.connect() as conn, pytest.raises(DBAPIError):
            conn.execute(text(statement))
    with fresh_engine.begin() as conn:  # status and description stay editable
        conn.execute(text("UPDATE portfolio_accounts SET description = 'notes'"))


def test_database_rejects_unbalanced_entries_and_late_lines(books, fresh_engine):
    account = books.account("Controlled test A")
    posted = books.post(
        account, "fee", "fee-1", {"kind": "FEE", "currency": "USD", "amount": "1.00"}
    )

    def raw_line(session, entry_id, sequence, side, amount, code=chart.CASH):
        session.add(
            JournalLineRecord(
                entry_id=entry_id,
                portfolio_account_id=account,
                sequence=sequence,
                line_number=99 if side == "CREDIT" else 98,
                account_code=code,
                currency="USD",
                side=side,
                amount=D(amount),
                functional_amount=D(amount),
                rate_basis="FUNCTIONAL",
                fx_rate=D(1),
            )
        )

    # A line added to an entry posted in an earlier transaction is refused.
    with books.factory() as session:
        raw_line(session, posted.entry_id, posted.sequence, "DEBIT", "1.00")
        with pytest.raises(DBAPIError, match="is posted"):
            session.flush()
    # An entry whose lines do not balance fails at COMMIT (deferred R1/R2 check).
    with books.factory() as session:
        entry = _raw_entry(session, account, 3, "raw")
        session.flush()
        raw_line(session, entry.id, 3, "DEBIT", "5.00", chart.FEES)
        raw_line(session, entry.id, 3, "CREDIT", "4.99")
        session.flush()
        with pytest.raises(DBAPIError, match="R1"):
            session.commit()
    # A sequence gap is refused.
    with books.factory() as session:
        _raw_entry(session, account, 9, "gap")
        with pytest.raises(DBAPIError, match="next number"):
            session.flush()
    _reconciled(books, account)


# ------------------------------------------------------------------ lifecycle vector
def test_long_and_short_lifecycle_vector_reconciles(books):
    account = books.account("Controlled test A")
    aaa = books.asset("AAA", bars=(150.0, 0.0))  # latest unadjusted close 149.70
    bbb = books.asset("BBB", bars=(22.0, 0.0))  # latest close 21.70

    books.trade(account, "long_purchase", "buy-aaa", aaa, "100", "150.25", "1.00", "0.05")
    assert _figures(books, account).cash == D("84973.95")

    first = books.ctx.valuation.checkpoint(account, actor="test")
    assert first.valuation_status is ValuationStatus.COMPLETE
    assert first.equity == D("99943.95")  # 84,973.95 + 100 x 149.70
    _reconciled(books, account)

    later(books, 30)
    books.trade(account, "long_sale", "sell-aaa", aaa, "40", "151.00")
    totals = _figures(books, account)
    assert totals.realized_pnl == D("30.00")  # 6,040 - 6,010
    assert totals.unrealized_pnl == D("-33.00")  # -55 with 40% released
    books.trade(account, "short_open", "short-bbb", bbb, "50", "20.00", "1.00")
    totals = _figures(books, account)
    assert totals.restricted_cash == D("1000.00")
    assert totals.cash == D("91012.95")  # proceeds are not available cash
    assert totals.equity == D("99994.95")
    books.post(
        account,
        "financing_cost",
        "borrow-bbb",
        {"kind": "BORROW", "currency": "USD", "amount": "2.50", "asset_id": str(bbb)},
    )
    assert _figures(books, account).accrued_costs == D("2.50")
    books.post(
        account,
        "accrual_settlement",
        "pay-borrow",
        {"payable": "FINANCING", "currency": "USD", "amount": "2.50"},
    )

    later(books, 30)
    second = books.ctx.valuation.checkpoint(account, actor="test")
    assert second.valuation_status is ValuationStatus.COMPLETE
    assert second.equity == D("99907.45")  # short marked at 21.70: -85.00 unrealized
    _reconciled(books, account)

    later(books, 30)
    books.trade(account, "short_cover", "cover-bbb", bbb, "50", "21.00", "1.00")
    totals = _figures(books, account)
    assert totals.realized_pnl == D("-20.00")  # +30 on AAA, -50 on BBB
    assert totals.unrealized_pnl == D("-33.00")
    assert totals.total_costs == D("5.55")
    assert totals.cash == D("90959.45")
    assert totals.restricted_cash == 0
    assert totals.short_liability == 0
    assert totals.equity == totals.equity_by_components == D("99941.45")
    assert totals.investment_return == D("-58.55")
    _reconciled(books, account)

    third = books.ctx.valuation.checkpoint(account, actor="test")
    assert third.equity == D("99941.45")
    _reconciled(books, account)
    with session_scope(books.factory) as session:
        snap = session.get(PortfolioSnapshotRecord, third.snapshot_id)
        assert snap is not None
        positions = session.scalars(
            select(PortfolioSnapshotPositionRecord).where(
                PortfolioSnapshotPositionRecord.snapshot_id == snap.id
            )
        ).all()
        assert [
            (p.symbol, p.quantity, p.carried_value, p.price_source, p.marked) for p in positions
        ] == [("AAA", D(60), D("8982.00"), "BAR_CLOSE", True)]
        assert positions[0].price_provenance["table"] == "market_bars"


def test_checkpoint_is_idempotent_and_ordered(books):
    account = books.account("Controlled test A")
    first = books.ctx.valuation.checkpoint(account, actor="test")
    again = books.ctx.valuation.checkpoint(account, as_of=first.as_of, actor="test")
    assert again.snapshot_id == first.snapshot_id
    assert not again.created
    with pytest.raises(PostingError, match="later snapshot"):
        books.ctx.valuation.checkpoint(account, as_of=NOW.replace(hour=10), actor="test")


# ------------------------------------------------------------------ idempotency and reversal
def test_duplicate_events_post_once_and_conflicting_reuse_is_refused(books):
    account = books.account("Controlled test A")
    params = {"kind": "COMMISSION", "currency": "USD", "amount": "1.25"}
    first = books.post(account, "fee", "evt-1", params)
    second = books.post(account, "fee", "evt-1", params)
    assert first.created
    assert not second.created
    assert second.entry_id == first.entry_id
    with pytest.raises(IdempotencyConflictError):
        books.post(account, "fee", "evt-1", {**params, "amount": "1.26"})
    assert _figures(books, account).commissions == D("1.25")


def test_reversal_restores_the_books_and_preserves_history(books):
    account = books.account("Controlled test A")
    aaa = books.asset("AAA")
    buy = books.trade(account, "long_purchase", "buy", aaa, "10", "10.00", "1.00")
    before = _figures(books, account)
    reversal = books.post(
        account, "reversal", "rev-buy", {"entry_id": str(buy.entry_id), "reason": "test correction"}
    )
    after = _figures(books, account)
    assert after.cash == D("100000.00")
    assert after.equity == D("100000.00")
    assert before.cash == D("99899.00")
    with pytest.raises(PostingError, match="already been reversed"):
        books.post(
            account, "reversal", "rev-buy-2", {"entry_id": str(buy.entry_id), "reason": "again"}
        )
    with pytest.raises(PostingError, match="cannot be reversed"):
        books.post(
            account, "reversal", "rev-rev", {"entry_id": str(reversal.entry_id), "reason": "undo"}
        )
    # Both entries remain; the original is unchanged.
    with session_scope(books.factory) as session:
        lines = session.scalar(
            select(func.count())
            .select_from(JournalLineRecord)
            .where(JournalLineRecord.entry_id == buy.entry_id)
        )
        assert lines == 4
    sell = books.trade(account, "long_purchase", "buy-2", aaa, "5", "10.00")
    books.trade(account, "long_sale", "sell-2", aaa, "5", "11.00")
    with pytest.raises(PostingError, match="later entries"):
        books.post(
            account, "reversal", "rev-buy-3", {"entry_id": str(sell.entry_id), "reason": "unsafe"}
        )
    _reconciled(books, account)


# ------------------------------------------------------------------ concurrency
def test_concurrent_postings_serialize_without_duplicates_or_gaps(books):
    account = books.account("Controlled test A")
    barrier = threading.Barrier(8)

    def worker(n: int) -> list[bool]:
        barrier.wait()
        created = []
        for i in range(5):
            created.append(
                books.post(
                    account,
                    "fee",
                    f"w{n}-{i}",
                    {"kind": "FEE", "currency": "USD", "amount": "0.01"},
                ).created
            )
        created.append(
            books.post(
                account, "fee", "shared", {"kind": "FEE", "currency": "USD", "amount": "0.50"}
            ).created
        )
        return created

    with ThreadPoolExecutor(8) as pool:
        results = [r for rs in pool.map(worker, range(8)) for r in rs]
    assert results.count(True) == 41  # 40 distinct events + the shared one once
    with session_scope(books.factory) as session:
        sequences = session.scalars(
            select(JournalEntryRecord.sequence).order_by(JournalEntryRecord.sequence)
        ).all()
    assert sequences == list(range(1, 43))
    assert _figures(books, account).fees == D("0.90")
    _reconciled(books, account)


def test_concurrent_purchases_never_overdraw_cash(books):
    account = books.account("Controlled test A", funding="1000.00")
    aaa = books.asset("AAA")
    barrier = threading.Barrier(10)

    def worker(n: int) -> str:
        barrier.wait()
        try:
            books.trade(account, "long_purchase", f"buy-{n}", aaa, "1", "150.00")
        except PostingError:
            return "refused"
        return "posted"

    with ThreadPoolExecutor(10) as pool:
        outcomes = list(pool.map(worker, range(10)))
    assert outcomes.count("posted") == 6
    totals = _figures(books, account)
    assert totals.cash == D("100.00")
    with session_scope(books.factory) as session:
        state = load_state(session, account, "USD")
        assert state.positions[aaa].quantity == 6
    _reconciled(books, account)


# ------------------------------------------------------------------ multi-currency
def test_multi_currency_conversion_translation_and_realized_fx(books):
    account = books.account("Controlled test A")
    books.rate("EUR", "1.0850", at=NOW)
    books.post(
        account,
        "fx_conversion",
        "buy-eur",
        {
            "sell_currency": "USD",
            "sell_amount": "10850.00",
            "buy_currency": "EUR",
            "buy_amount": "10000.00",
        },
    )
    later(books, 10)
    books.rate("EUR", "1.1000")
    snapshot = books.ctx.valuation.checkpoint(account, actor="test")
    assert snapshot.valuation_status is ValuationStatus.COMPLETE
    assert snapshot.equity == D("100150.00")
    books.post(
        account,
        "fx_conversion",
        "sell-eur",
        {
            "sell_currency": "EUR",
            "sell_amount": "4000.00",
            "buy_currency": "USD",
            "buy_amount": "4420.00",
        },
    )
    totals = _figures(books, account)
    assert totals.realized_fx == D("80.00")
    assert totals.unrealized_fx == D("90.00")
    assert totals.equity == D("100170.00")
    assert totals.fx_clearing == 0
    with session_scope(books.factory) as session:
        executed = session.scalars(
            select(FxRateRecord).where(FxRateRecord.source == FxRateSource.CONVERSION)
        ).all()
        assert sorted(str(r.rate) for r in executed) == ["0.921658986175", "1.105000000000"]
        eur = session.get(CurrencyBalanceRecord, (account, chart.CASH, "EUR"))
        assert eur is not None
        assert eur.balance == D("6000.00")
    _reconciled(books, account)


def test_executed_conversion_rates_never_value_anything(books):
    account = books.account("Controlled test A")
    books.rate("EUR", "1.0850", at=NOW)
    books.post(
        account,
        "fx_conversion",
        "buy-eur",
        {
            "sell_currency": "USD",
            "sell_amount": "108.50",
            "buy_currency": "EUR",
            "buy_amount": "100.00",
        },
    )
    later(books, 60 * 24 * 5)  # the fixture rate is now stale; only the CONVERSION row is new
    result = books.ctx.valuation.checkpoint(account, actor="test")
    assert result.valuation_status is ValuationStatus.STALE
    assert [i["kind"] for i in result.issues] == ["RATE_STALE"]
    with pytest.raises(PostingError, match="no fresh EUR/USD rate"):
        books.post(
            account,
            "fee",
            "eur-fee",
            {"kind": "FEE", "currency": "EUR", "amount": "1.00", "accrue": True},
        )


# ------------------------------------------------------------------ stale valuations
def test_stale_and_missing_prices_flag_the_snapshot_and_keep_the_last_mark(books):
    account = books.account("Controlled test A")
    old = books.asset("OLD", bars=(50.0, 0.0), bars_as_of=NOW.replace(day=1))
    none = books.asset("NONE")
    books.trade(account, "long_purchase", "buy-old", old, "10", "50.00")
    books.trade(account, "long_purchase", "buy-none", none, "10", "20.00")
    result = books.ctx.valuation.checkpoint(account, actor="test")
    assert result.valuation_status is ValuationStatus.INCOMPLETE
    kinds = {(i["symbol"], i["kind"]) for i in result.issues}
    assert kinds == {("OLD", "PRICE_STALE"), ("NONE", "PRICE_MISSING")}
    assert result.valuation_entry_id is None  # nothing was marked
    totals = _figures(books, account)
    assert totals.unrealized_pnl == 0
    with session_scope(books.factory) as session:
        positions = {
            p.symbol: p
            for p in session.scalars(
                select(PortfolioSnapshotPositionRecord).where(
                    PortfolioSnapshotPositionRecord.snapshot_id == result.snapshot_id
                )
            )
        }
    assert positions["OLD"].price == D("49.7")  # reported with provenance, not used
    assert positions["OLD"].price_status.value == "STALE"
    assert not positions["OLD"].marked
    assert positions["NONE"].price is None
    assert positions["NONE"].carried_value == D("200.00")  # kept at cost
    _reconciled(books, account)  # a flagged valuation is not an accounting error


# ------------------------------------------------------------------ reconciliation detects
def test_reconciliation_reports_tampering_as_high_priority_errors(books, fresh_engine):
    account = books.account("Controlled test A")
    books.post(account, "fee", "fee-1", {"kind": "FEE", "currency": "USD", "amount": "1.00"})
    _reconciled(books, account)
    # The balance cache is not authoritative; a changed cache is detected, never "fixed".
    with fresh_engine.begin() as conn:
        conn.execute(
            text("UPDATE currency_balances SET balance = balance + 1 WHERE account_code = '1000'")
        )
    summary = books.ctx.reconciliation.run(account, actor="test")
    assert summary.status is ReconciliationStatus.FAILED
    assert {c.code for c in summary.failures} == {"CURRENCY_BALANCES"}
    # A line slipped in behind the guards (triggers disabled) breaks the balance checks.
    with fresh_engine.begin() as conn:
        conn.execute(text("ALTER TABLE journal_lines DISABLE TRIGGER USER"))
        conn.execute(
            text(
                "INSERT INTO journal_lines (entry_id, portfolio_account_id, sequence, line_number,"
                " account_code, currency, side, amount, functional_amount, rate_basis, fx_rate)"
                " SELECT id, portfolio_account_id, sequence, 50, '1000', 'USD', 'DEBIT', 7, 7,"
                " 'FUNCTIONAL', 1 FROM journal_entries WHERE event_id = 'fee-1'"
            )
        )
        conn.execute(text("ALTER TABLE journal_lines ENABLE TRIGGER USER"))
    summary = books.ctx.reconciliation.run(account, actor="test")
    failed = {c.code for c in summary.failures}
    assert {"ENTRY_BALANCE", "TRIAL_BALANCE", "CURRENCY_BALANCES", "EQUITY"} <= failed
    with session_scope(books.factory) as session:
        results = session.scalars(
            select(ReconciliationResultRecord).where(
                ReconciliationResultRecord.run_id == summary.run_id,
                ReconciliationResultRecord.status == CheckStatus.FAIL,
            )
        ).all()
        assert results
        assert all(r.severity is Severity.HIGH for r in results)
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "accounting.reconciliation_failed")
            )
            == 2
        )
        # Nothing was corrected: the tampered cache value is still there.
        cached = session.get(CurrencyBalanceRecord, (account, chart.CASH, "USD"))
        assert cached is not None
        assert cached.balance == D("99999.00") + 1


def test_posting_templates_are_recorded_once_and_verified(books):
    account = books.account("Controlled test A")
    books.post(account, "fee", "fee-1", {"kind": "FEE", "currency": "USD", "amount": "1.00"})
    with session_scope(books.factory) as session:
        rows = session.scalars(
            select(PostingTemplateRecord).order_by(PostingTemplateRecord.code)
        ).all()
        assert [(r.code, r.version) for r in rows] == [("fee", 1), ("opening_funding", 1)]
        assert rows[0].definition_sha256 == TEMPLATES["fee"].definition_sha256()
        codes = {r.code for r in session.execute(text("SELECT code FROM ledger_accounts")).all()}
        assert codes == set(chart.CHART)

"""Double-entry accounting tables (spec 16, 21; Phase 6A, migration 0013).

Immutable (append-only triggers, no TRUNCATE): ``currencies``, ``fx_rates``,
``instrument_specs``, ``ledger_accounts``, ``posting_templates``, ``journal_entries``,
``journal_lines``, ``asset_quantity_ledger``, ``reconciliation_runs``,
``reconciliation_results``. Corrections are reversal entries, never edits.

``currency_balances`` is a cache maintained in the posting transaction for fast reads. It is
never authoritative: reconciliation recomputes it from ``journal_lines`` and reports any
difference as a high-priority accounting error.

Database-side defence in depth (migration 0013):

* ``atlas_journal_entry_guard``: an entry's posting transaction id is the current one and
  its sequence is exactly the account's next number (gapless).
* ``atlas_journal_line_guard``: a line can only be added in its entry's own transaction and
  must satisfy the chart's line rules and currency precision.
* ``atlas_journal_entry_balanced`` (deferred to COMMIT): every entry has >= 2 lines, each
  currency balances (R1) and functional values balance (R2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.accounting.chart import AssetRule
from app.accounting.enums import (
    AccountCategory,
    AccountType,
    CheckStatus,
    EntrySource,
    EntryType,
    FxRateSource,
    InstrumentSpecSource,
    MovementType,
    ReconciliationStatus,
    Severity,
    Side,
)
from app.accounting.ledger import RateBasis
from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum

AMOUNT = Numeric(28, 8)
QUANTITY = Numeric(38, 18)
PRICE = Numeric(30, 10)
RATE = Numeric(30, 12)


def _currency_fk(column: str = "currency") -> Mapped[str]:
    return mapped_column(String(3), ForeignKey("currencies.code", ondelete="RESTRICT"))


class CurrencyRecord(Base):
    __tablename__ = "currencies"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Z]{3}$'", name="code_iso"),
        CheckConstraint("minor_units BETWEEN 0 AND 8", name="minor_units_range"),
    )

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    minor_units: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FxRateRecord(Base):
    """A timestamped exchange rate: 1 ``base_currency`` = ``rate`` ``quote_currency``."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "source", "observed_at"),
        CheckConstraint("base_currency <> quote_currency", name="distinct_currencies"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("source <> 'PROVIDER' OR provider IS NOT NULL", name="provider_named"),
        CheckConstraint("length(provenance) > 0", name="provenance_present"),
        Index(None, "base_currency", "quote_currency", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    base_currency: Mapped[str] = _currency_fk()
    quote_currency: Mapped[str] = _currency_fk()
    rate: Mapped[Decimal] = mapped_column(RATE)
    observed_at: Mapped[datetime]
    source: Mapped[FxRateSource] = mapped_column(str_enum(FxRateSource, "fx_rate_source"))
    provider: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime]
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InstrumentSpecRecord(Base):
    """Quantity precision and lot size of an instrument, effective from ``valid_from``."""

    __tablename__ = "instrument_specs"
    __table_args__ = (
        UniqueConstraint("asset_id", "valid_from"),
        CheckConstraint("quantity_increment > 0", name="increment_positive"),
        CheckConstraint("min_quantity >= 0", name="min_quantity_non_negative"),
        CheckConstraint("price_increment IS NULL OR price_increment > 0", name="tick_positive"),
        CheckConstraint("multiplier > 0", name="multiplier_positive"),
        CheckConstraint("length(provenance) > 0", name="provenance_present"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    currency: Mapped[str] = _currency_fk()
    quantity_increment: Mapped[Decimal] = mapped_column(QUANTITY)
    min_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price_increment: Mapped[Decimal | None] = mapped_column(PRICE)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(20, 8), server_default="1")
    valid_from: Mapped[datetime]
    source: Mapped[InstrumentSpecSource] = mapped_column(
        str_enum(InstrumentSpecSource, "instrument_spec_source")
    )
    provenance: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LedgerAccountRecord(Base):
    """The chart of accounts (mirrors ``app.accounting.chart.CHART``)."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        CheckConstraint("code ~ '^[0-9]{4}$'", name="code_format"),
        CheckConstraint("allows_amount OR allows_functional_only", name="takes_some_line"),
    )

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    account_type: Mapped[AccountType] = mapped_column(str_enum(AccountType, "account_type"))
    normal_side: Mapped[Side] = mapped_column(str_enum(Side, "ledger_side"))
    category: Mapped[AccountCategory] = mapped_column(str_enum(AccountCategory, "account_category"))
    asset_rule: Mapped[AssetRule] = mapped_column(str_enum(AssetRule, "ledger_asset_rule"))
    allows_amount: Mapped[bool] = mapped_column(Boolean)
    allows_functional_only: Mapped[bool] = mapped_column(Boolean)
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PostingTemplateRecord(Base):
    __tablename__ = "posting_templates"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("definition_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    entry_type: Mapped[EntryType] = mapped_column(str_enum(EntryType, "journal_entry_type"))
    description: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict[str, Any]]
    definition_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JournalEntryRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "sequence"),
        UniqueConstraint("portfolio_account_id", "event_id"),
        UniqueConstraint("reverses_entry_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("length(event_id) > 0", name="event_id_present"),
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="request_sha256_format"),
        CheckConstraint(
            "(entry_type = 'REVERSAL') = (reverses_entry_id IS NOT NULL)",
            name="reversal_iff_reference",
        ),
        CheckConstraint(
            "entry_type NOT IN ('OPENING_FUNDING', 'VALUATION') OR source = 'SYSTEM'",
            name="system_entries_from_system",
        ),
        # One opening funding per account, ever.
        Index(
            "uq_journal_entries_opening_funding",
            "portfolio_account_id",
            unique=True,
            postgresql_where=text("entry_type = 'OPENING_FUNDING'"),
        ),
        Index(None, "portfolio_account_id", "effective_at"),
        Index(None, "entry_type"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[str] = mapped_column(String(160))
    request_sha256: Mapped[str] = mapped_column(String(64))
    entry_type: Mapped[EntryType] = mapped_column(str_enum(EntryType, "journal_entry_type"))
    template_id: Mapped[int] = mapped_column(
        ForeignKey("posting_templates.id", ondelete="RESTRICT")
    )
    source: Mapped[EntrySource] = mapped_column(str_enum(EntrySource, "journal_entry_source"))
    source_ref: Mapped[str | None] = mapped_column(String(255))
    functional_currency: Mapped[str] = _currency_fk()
    effective_at: Mapped[datetime]
    posted_at: Mapped[datetime]
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    description: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_by: Mapped[str] = mapped_column(String(128))
    posting_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))


class JournalLineRecord(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        UniqueConstraint("entry_id", "line_number"),
        CheckConstraint("line_number >= 1", name="line_number_positive"),
        CheckConstraint("amount >= 0 AND functional_amount >= 0", name="non_negative"),
        CheckConstraint("amount > 0 OR functional_amount > 0", name="carries_value"),
        CheckConstraint("fx_rate IS NULL OR fx_rate >= 0", name="rate_non_negative"),
        CheckConstraint(
            "rate_basis <> 'FUNCTIONAL' OR "
            "(fx_rate = 1 AND functional_amount = amount AND fx_rate_id IS NULL)",
            name="functional_basis_identity",
        ),
        CheckConstraint(
            "rate_basis <> 'SPOT' OR (fx_rate_id IS NOT NULL AND fx_rate > 0)",
            name="spot_basis_references_rate",
        ),
        CheckConstraint(
            "(rate_basis = 'NONE') = (fx_rate IS NULL)", name="rate_recorded_unless_none"
        ),
        CheckConstraint("rate_basis <> 'NONE' OR amount = 0", name="none_basis_functional_only"),
        Index(None, "portfolio_account_id", "account_code", "currency"),
        Index(None, "asset_id"),
        Index(None, "fx_rate_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    line_number: Mapped[int] = mapped_column(SmallInteger)
    account_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("ledger_accounts.code", ondelete="RESTRICT")
    )
    currency: Mapped[str] = _currency_fk()
    side: Mapped[Side] = mapped_column(str_enum(Side, "ledger_side"))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    functional_amount: Mapped[Decimal] = mapped_column(AMOUNT)
    rate_basis: Mapped[RateBasis] = mapped_column(str_enum(RateBasis, "ledger_rate_basis"))
    fx_rate: Mapped[Decimal | None] = mapped_column(RATE)
    fx_rate_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fx_rates.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    memo: Mapped[str | None] = mapped_column(String(255))


class AssetQuantityRecord(Base):
    """The asset quantity subledger: one row per position change, with running totals."""

    __tablename__ = "asset_quantity_ledger"
    __table_args__ = (
        UniqueConstraint("entry_id", "asset_id"),
        UniqueConstraint("portfolio_account_id", "asset_id", "sequence"),
        CheckConstraint("quantity_delta <> 0", name="moves_quantity"),
        CheckConstraint("cost_after >= 0 AND functional_cost_after >= 0", name="cost_non_negative"),
        CheckConstraint(
            "quantity_after <> 0 OR (cost_after = 0 AND functional_cost_after = 0)",
            name="flat_has_no_cost",
        ),
        CheckConstraint("price IS NULL OR price > 0", name="price_positive"),
        Index(None, "portfolio_account_id", "asset_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    movement_type: Mapped[MovementType] = mapped_column(
        str_enum(MovementType, "quantity_movement_type")
    )
    currency: Mapped[str] = _currency_fk()
    quantity_delta: Mapped[Decimal] = mapped_column(QUANTITY)
    quantity_after: Mapped[Decimal] = mapped_column(QUANTITY)
    cost_delta: Mapped[Decimal] = mapped_column(AMOUNT)
    cost_after: Mapped[Decimal] = mapped_column(AMOUNT)
    functional_cost_delta: Mapped[Decimal] = mapped_column(AMOUNT)
    functional_cost_after: Mapped[Decimal] = mapped_column(AMOUNT)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT)
    realized_pnl_functional: Mapped[Decimal] = mapped_column(AMOUNT)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CurrencyBalanceRecord(Base):
    """Cached balance per portfolio account, ledger account and currency (debit-positive)."""

    __tablename__ = "currency_balances"
    __table_args__ = (
        PrimaryKeyConstraint("portfolio_account_id", "account_code", "currency"),
        ForeignKeyConstraint(
            ["portfolio_account_id"], ["portfolio_accounts.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(["account_code"], ["ledger_accounts.code"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["currency"], ["currencies.code"], ondelete="RESTRICT"),
        CheckConstraint("line_count >= 1", name="line_count_positive"),
    )

    portfolio_account_id: Mapped[uuid.UUID]
    account_code: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(3))
    balance: Mapped[Decimal] = mapped_column(AMOUNT)
    functional_balance: Mapped[Decimal] = mapped_column(AMOUNT)
    line_count: Mapped[int] = mapped_column(BigInteger)
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime]


class ReconciliationRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint("checks_failed >= 0 AND checks_failed <= checks_total", name="counts"),
        CheckConstraint(
            "(status = 'PASSED') = (checks_failed = 0)", name="status_matches_failures"
        ),
        Index(None, "portfolio_account_id", "finished_at"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    entry_sequence: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[ReconciliationStatus] = mapped_column(
        str_enum(ReconciliationStatus, "reconciliation_status")
    )
    checks_total: Mapped[int] = mapped_column(Integer)
    checks_failed: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]


class ReconciliationResultRecord(Base):
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        CheckConstraint("status = 'PASS' OR severity = 'HIGH'", name="failures_are_high"),
        Index(None, "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT")
    )
    check_code: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    expected: Mapped[Decimal | None] = mapped_column(Numeric(40, 18))
    actual: Mapped[Decimal | None] = mapped_column(Numeric(40, 18))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(40, 18))
    status: Mapped[CheckStatus] = mapped_column(
        str_enum(CheckStatus, "reconciliation_check_status")
    )
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "reconciliation_severity"))
    message: Mapped[str] = mapped_column(Text)

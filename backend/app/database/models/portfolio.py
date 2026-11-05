"""Portfolio snapshots (spec 13, 16.6, 21; Phase 6A, migration 0013). Append-only.

A snapshot is written by a valuation checkpoint after its VALUATION entry (if any) is
posted, in the same transaction. Every figure is derived from the ledger as of
``entry_sequence`` in the functional currency; the positions record the price and FX rate
used for each, with provenance, or why none was usable (``price_status``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.accounting.enums import PriceStatus, ValuationStatus
from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum

AMOUNT = Numeric(28, 8)

SNAPSHOT_FIGURES = (
    "contributed_capital",
    "cash",
    "restricted_cash",
    "long_value",
    "short_liability",
    "accrued_costs",
    "fx_translation",
    "total_assets",
    "total_liabilities",
    "equity",
    "realized_pnl",
    "unrealized_pnl",
    "realized_fx",
    "unrealized_fx",
    "commissions",
    "fees",
    "borrow_costs",
    "funding_costs",
    "investment_return",
    "available_capital",
    "long_exposure",
    "short_exposure",
    "gross_exposure",
    "net_exposure",
)


class PortfolioSnapshotRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "as_of"),
        CheckConstraint("entry_sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("total_assets - total_liabilities = equity", name="accounting_equation"),
        CheckConstraint(
            "equity - contributed_capital = investment_return", name="return_excludes_funding"
        ),
        Index(None, "portfolio_account_id", "as_of"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    as_of: Mapped[datetime]
    entry_sequence: Mapped[int] = mapped_column(BigInteger)
    valuation_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    valuation_status: Mapped[ValuationStatus] = mapped_column(
        str_enum(ValuationStatus, "valuation_status")
    )
    functional_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    contributed_capital: Mapped[Decimal] = mapped_column(AMOUNT)
    cash: Mapped[Decimal] = mapped_column(AMOUNT)
    restricted_cash: Mapped[Decimal] = mapped_column(AMOUNT)
    long_value: Mapped[Decimal] = mapped_column(AMOUNT)
    short_liability: Mapped[Decimal] = mapped_column(AMOUNT)
    accrued_costs: Mapped[Decimal] = mapped_column(AMOUNT)
    fx_translation: Mapped[Decimal] = mapped_column(AMOUNT)
    total_assets: Mapped[Decimal] = mapped_column(AMOUNT)
    total_liabilities: Mapped[Decimal] = mapped_column(AMOUNT)
    equity: Mapped[Decimal] = mapped_column(AMOUNT)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT)
    unrealized_pnl: Mapped[Decimal] = mapped_column(AMOUNT)
    realized_fx: Mapped[Decimal] = mapped_column(AMOUNT)
    unrealized_fx: Mapped[Decimal] = mapped_column(AMOUNT)
    commissions: Mapped[Decimal] = mapped_column(AMOUNT)
    fees: Mapped[Decimal] = mapped_column(AMOUNT)
    borrow_costs: Mapped[Decimal] = mapped_column(AMOUNT)
    funding_costs: Mapped[Decimal] = mapped_column(AMOUNT)
    investment_return: Mapped[Decimal] = mapped_column(AMOUNT)
    available_capital: Mapped[Decimal] = mapped_column(AMOUNT)
    long_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    short_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    gross_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    net_exposure: Mapped[Decimal] = mapped_column(AMOUNT)
    cash_by_currency: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PortfolioSnapshotPositionRecord(Base):
    __tablename__ = "portfolio_snapshot_positions"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "asset_id"),
        CheckConstraint("quantity <> 0", name="open_position"),
        CheckConstraint(
            "(price_status = 'MISSING') = (price IS NULL)", name="price_iff_not_missing"
        ),
        CheckConstraint("price_status = 'FRESH' OR NOT marked", name="only_fresh_prices_marked"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    cost_basis: Mapped[Decimal] = mapped_column(AMOUNT)
    average_price: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    # The price observed for this checkpoint and where it came from (None when missing).
    price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    price_status: Mapped[PriceStatus] = mapped_column(str_enum(PriceStatus, "price_status"))
    price_source: Mapped[str | None] = mapped_column(String(32))
    price_observed_at: Mapped[datetime | None]
    price_provenance: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # Whether this checkpoint re-marked the position (fresh price and, if foreign, rate).
    marked: Mapped[bool]
    # Valuation as carried in the ledger after the checkpoint, in the asset currency:
    # cost + adjustment for a long, proceeds + adjustment for a short liability.
    carried_value: Mapped[Decimal] = mapped_column(AMOUNT)
    valuation_adjustment: Mapped[Decimal] = mapped_column(AMOUNT)
    carried_value_functional: Mapped[Decimal] = mapped_column(AMOUNT)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    fx_rate_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fx_rates.id", ondelete="RESTRICT")
    )

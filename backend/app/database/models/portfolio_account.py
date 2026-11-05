"""Portfolio accounts (spec sections 13, 16, 21).

A portfolio account is the identity and lock target for portfolio-level state.
It deliberately has **no balance columns**: the double-entry ledger (Phase 6) is
the authoritative source for cash, positions, and equity, and any summary is
derived from it (spec section 16.6).

``account_type`` is constrained to ``PAPER`` at the database level as a
defence-in-depth guard for paper-only isolation (spec section 24.1).

Phase 6A (migration 0013) adds:

* ``purpose``: PRIMARY is the paper account Atlas trades (opening funding, valuation and, in a
  later phase, simulated executions). CONTROLLED_TEST accounts exist so posting templates
  can be exercised with controlled test transactions without ever touching the primary
  account's books; the posting service refuses test transactions anywhere else.
* ``base_currency`` references ``currencies``; it is the account's functional (reporting)
  currency. ``atlas_portfolio_account_guard`` makes ``account_type``, ``base_currency`` and
  ``purpose`` immutable and forbids deleting an account.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class PortfolioAccountType(StrEnum):
    PAPER = "PAPER"


class PortfolioAccountPurpose(StrEnum):
    PRIMARY = "PRIMARY"
    CONTROLLED_TEST = "CONTROLLED_TEST"


class PortfolioAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class PortfolioAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portfolio_accounts"
    __table_args__ = (
        CheckConstraint(
            "base_currency = upper(base_currency) AND length(base_currency) = 3",
            name="base_currency_iso",
        ),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True)
    account_type: Mapped[PortfolioAccountType] = mapped_column(
        str_enum(PortfolioAccountType, "portfolio_account_type"),
        default=PortfolioAccountType.PAPER,
        server_default="PAPER",
    )
    base_currency: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.code", ondelete="RESTRICT"),
        default="USD",
        server_default="USD",
    )
    purpose: Mapped[PortfolioAccountPurpose] = mapped_column(
        str_enum(PortfolioAccountPurpose, "portfolio_account_purpose"),
        default=PortfolioAccountPurpose.PRIMARY,
        server_default="PRIMARY",
    )
    status: Mapped[PortfolioAccountStatus] = mapped_column(
        str_enum(PortfolioAccountStatus, "portfolio_account_status"),
        default=PortfolioAccountStatus.ACTIVE,
        server_default="ACTIVE",
    )
    description: Mapped[str | None] = mapped_column(String(1024))

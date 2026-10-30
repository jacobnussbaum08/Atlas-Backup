"""Portfolio accounts (spec sections 13, 16, 21).

A portfolio account is the identity and lock target for portfolio-level state.
It deliberately has **no balance columns**: the double-entry ledger (Phase 6) is
the authoritative source for cash, positions, and equity, and any summary is
derived from it (spec section 16.6).

``account_type`` is constrained to ``PAPER`` at the database level as a
defence-in-depth guard for paper-only isolation (spec section 24.1).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class PortfolioAccountType(StrEnum):
    PAPER = "PAPER"


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
    base_currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    status: Mapped[PortfolioAccountStatus] = mapped_column(
        str_enum(PortfolioAccountStatus, "portfolio_account_status"),
        default=PortfolioAccountStatus.ACTIVE,
        server_default="ACTIVE",
    )
    description: Mapped[str | None] = mapped_column(String(1024))

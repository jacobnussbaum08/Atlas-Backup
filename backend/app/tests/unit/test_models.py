"""Static checks on the ORM metadata that protect database conventions."""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Enum, Float

from app.database.models import Base, PortfolioAccount, PortfolioAccountType

TABLES = Base.metadata.sorted_tables


def test_expected_foundation_tables_exist():
    assert set(Base.metadata.tables) == {
        "assets",
        "asset_identifiers",
        "portfolio_accounts",
        "audit_logs",
    }


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_all_timestamps_are_timezone_aware(table):
    naive = [c.name for c in table.columns if isinstance(c.type, DateTime) and not c.type.timezone]
    assert naive == []


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_no_floating_point_columns(table):
    assert [c.name for c in table.columns if isinstance(c.type, Float)] == []


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_every_table_has_a_primary_key_and_named_constraints(table):
    assert table.primary_key.columns
    for constraint in table.constraints:
        assert constraint.name, f"unnamed constraint on {table.name}"


def test_portfolio_accounts_hold_no_balances():
    """The ledger is authoritative; account rows must not carry cached money values."""
    columns = set(PortfolioAccount.__table__.columns.keys())
    forbidden = {"balance", "cash", "equity", "buying_power", "pnl"}
    assert not {c for c in columns if any(word in c for word in forbidden)}


def test_portfolio_account_type_is_paper_only():
    assert list(PortfolioAccountType) == [PortfolioAccountType.PAPER]
    column_type = PortfolioAccount.__table__.c.account_type.type
    assert isinstance(column_type, Enum)
    assert column_type.enums == ["PAPER"]

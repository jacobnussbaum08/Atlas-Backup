"""Database-level constraints: these must hold even for code that bypasses the ORM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database.models import (
    Asset,
    AssetClass,
    AssetIdentifier,
    PortfolioAccount,
    PortfolioAccountType,
)

pytestmark = pytest.mark.integration


def _expect_integrity_error(session, statement, params=None):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text(statement), params or {})


def test_asset_round_trip_with_server_defaults(session):
    asset = Asset(
        symbol="AAPL", asset_class=AssetClass.EQUITY, exchange_code="XNAS", quote_currency="USD"
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)

    assert asset.id is not None
    assert asset.status.value == "ACTIVE"
    assert asset.created_at.tzinfo is not None
    assert asset.created_at.utcoffset() == timedelta(0)


def test_asset_identity_is_unique_even_without_exchange(session):
    session.add(
        Asset(
            symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            quote_currency="USD",
            base_currency="EUR",
        )
    )
    session.flush()

    _expect_integrity_error(
        session,
        "INSERT INTO assets (symbol, asset_class, quote_currency, base_currency) "
        "VALUES ('EUR/USD', 'FOREX', 'USD', 'EUR')",
    )


@pytest.mark.parametrize(
    "values",
    [
        "('aapl', 'EQUITY', 'USD', NULL)",  # lowercase symbol
        "('', 'EQUITY', 'USD', NULL)",  # empty symbol
        "('AAPL', 'EQUITY', 'usd', NULL)",  # lowercase currency
        "('BTC/USD', 'CRYPTO', 'USD', NULL)",  # pair without base currency
        "('AAPL', 'BOND', 'USD', NULL)",  # unsupported asset class
    ],
)
def test_invalid_assets_are_rejected(session, values):
    _expect_integrity_error(
        session,
        f"INSERT INTO assets (symbol, asset_class, quote_currency, base_currency) VALUES {values}",
    )


def test_portfolio_accounts_are_paper_only(session):
    account = PortfolioAccount(name="Primary paper account")
    session.add(account)
    session.flush()
    session.refresh(account)
    assert account.account_type is PortfolioAccountType.PAPER
    assert account.base_currency == "USD"

    _expect_integrity_error(
        session, "INSERT INTO portfolio_accounts (name, account_type) VALUES ('Real', 'LIVE')"
    )
    _expect_integrity_error(
        session, "INSERT INTO portfolio_accounts (name, base_currency) VALUES ('Lower', 'usd')"
    )
    _expect_integrity_error(
        session, "INSERT INTO portfolio_accounts (name) VALUES ('Primary paper account')"
    )


def test_provider_symbol_maps_to_one_current_asset(session):
    first = Asset(
        symbol="SPY", asset_class=AssetClass.ETF, exchange_code="ARCX", quote_currency="USD"
    )
    second = Asset(
        symbol="SPY2", asset_class=AssetClass.ETF, exchange_code="ARCX", quote_currency="USD"
    )
    session.add_all([first, second])
    session.flush()

    session.add(AssetIdentifier(asset_id=first.id, provider="example", provider_symbol="SPY"))
    session.flush()

    _expect_integrity_error(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol) "
        "VALUES (:id, 'example', 'SPY')",
        {"id": second.id},
    )


def test_provider_symbol_may_be_reassigned_after_validity_ends(session):
    old = Asset(
        symbol="FB", asset_class=AssetClass.EQUITY, exchange_code="XNAS", quote_currency="USD"
    )
    new = Asset(
        symbol="META", asset_class=AssetClass.EQUITY, exchange_code="XNAS", quote_currency="USD"
    )
    session.add_all([old, new])
    session.flush()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    session.add_all(
        [
            AssetIdentifier(
                asset_id=old.id,
                provider="example",
                provider_symbol="X1",
                valid_from=start,
                valid_to=start + timedelta(days=365),
            ),
            AssetIdentifier(
                asset_id=new.id,
                provider="example",
                provider_symbol="X1",
                valid_from=start + timedelta(days=365),
            ),
        ]
    )
    session.flush()

    rows = session.scalars(
        select(AssetIdentifier).where(AssetIdentifier.provider_symbol == "X1")
    ).all()
    assert len(rows) == 2


def test_identifier_validity_period_must_be_ordered(session):
    asset = Asset(
        symbol="QQQ", asset_class=AssetClass.ETF, exchange_code="XNAS", quote_currency="USD"
    )
    session.add(asset)
    session.flush()

    _expect_integrity_error(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol, valid_from, valid_to) "
        "VALUES (:id, 'example', 'QQQ', now(), now() - interval '1 day')",
        {"id": asset.id},
    )


def test_assets_with_identifiers_cannot_be_deleted(session):
    asset = Asset(
        symbol="IWM", asset_class=AssetClass.ETF, exchange_code="ARCX", quote_currency="USD"
    )
    session.add(asset)
    session.flush()
    session.add(AssetIdentifier(asset_id=asset.id, provider="example", provider_symbol="IWM"))
    session.flush()

    _expect_integrity_error(session, "DELETE FROM assets WHERE id = :id", {"id": asset.id})

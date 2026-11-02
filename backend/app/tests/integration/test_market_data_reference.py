"""Provider registry tables and asset-provider mappings against real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.data_engine.providers.alpaca.capabilities import ALPACA_CAPABILITIES
from app.data_engine.providers.registry import available_providers
from app.database.models import (
    Asset,
    AssetClass,
    MappingStatus,
    MarketDataFeedRecord,
    MarketDataProviderRecord,
)
from app.database.repositories import AssetMappingRepository, MappingError

pytestmark = pytest.mark.integration

T0 = datetime(2020, 1, 1, tzinfo=UTC)


def _asset(session, symbol: str, asset_class=AssetClass.EQUITY) -> Asset:
    # Any seeded MIC: assets.exchange_code references exchanges (Phase 3A).
    asset = Asset(
        symbol=symbol, asset_class=asset_class, exchange_code="XNAS", quote_currency="USD"
    )
    session.add(asset)
    session.flush()
    return asset


def _asset_at(repo: AssetMappingRepository, symbol: str, at: datetime):
    mapping = repo.resolve("alpaca", symbol, at)
    assert mapping is not None, f"{symbol} unresolved at {at}"
    return mapping.asset_id


def _rejects(session, statement: str, params=None, error=IntegrityError):
    with pytest.raises(error), session.begin_nested():
        session.execute(text(statement), params or {})


# ------------------------------------------------------------------ reference data
def test_registered_providers_match_the_code_registry(session):
    codes = set(session.scalars(select(MarketDataProviderRecord.code)))
    assert codes == set(available_providers())


def test_seeded_feeds_match_declared_capabilities(session):
    rows = session.scalars(
        select(MarketDataFeedRecord).where(MarketDataFeedRecord.provider == "alpaca")
    ).all()
    seeded = {(r.code, r.coverage, r.delay_seconds) for r in rows}
    declared = {
        (f.code, f.coverage, int(f.delay.total_seconds())) for f in ALPACA_CAPABILITIES.feeds
    }
    assert seeded == declared


def test_feed_rows_require_a_registered_provider_and_valid_codes(session):
    _rejects(
        session,
        "INSERT INTO market_data_feeds (provider, code, display_name, coverage) "
        "VALUES ('nobody', 'iex', 'x', 'SINGLE_VENUE')",
    )
    _rejects(
        session,
        "INSERT INTO market_data_feeds (provider, code, display_name, coverage) "
        "VALUES ('alpaca', 'IEX', 'x', 'SINGLE_VENUE')",
    )
    _rejects(
        session,
        "INSERT INTO market_data_feeds (provider, code, display_name, coverage) "
        "VALUES ('alpaca', 'x1', 'x', 'PARTIAL')",
    )


def test_providers_with_feeds_cannot_be_deleted(session):
    _rejects(session, "DELETE FROM market_data_providers WHERE code = 'alpaca'")


# ------------------------------------------------------------------ mappings
def test_mapping_requires_registered_provider(session):
    asset = _asset(session, "MAPA")
    _rejects(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol) "
        "VALUES (:id, 'unregistered', 'MAPA')",
        {"id": asset.id},
    )


def test_register_and_resolve_at_time(session):
    repo = AssetMappingRepository(session)
    asset = _asset(session, "MAPB", AssetClass.ETF)

    mapping = repo.register(
        asset_id=asset.id,
        provider="alpaca",
        provider_symbol="MAPB",
        valid_from=T0,
        provider_exchange="ARCA",
    )

    assert mapping.status is MappingStatus.ACTIVE
    assert mapping.provider_exchange == "ARCA"
    assert _asset_at(repo, "MAPB", T0 + timedelta(days=1)) == asset.id
    assert repo.resolve("alpaca", "MAPB", T0 - timedelta(seconds=1)) is None
    current = repo.current_for_asset(asset.id, "alpaca")
    assert current is not None
    assert current.id == mapping.id
    assert [a.symbol for a, _ in repo.current_assets("alpaca") if a.symbol == "MAPB"] == ["MAPB"]


def test_symbol_reuse_resolves_to_the_asset_valid_at_each_time(session):
    """A ticker reused by a different company must not merge their histories."""
    repo = AssetMappingRepository(session)
    old, new = _asset(session, "OLDCO"), _asset(session, "NEWCO")
    handover = T0 + timedelta(days=365)

    first = repo.register(
        asset_id=old.id, provider="alpaca", provider_symbol="REUSE", valid_from=T0
    )
    repo.retire(first.id, handover)
    repo.register(asset_id=new.id, provider="alpaca", provider_symbol="REUSE", valid_from=handover)

    assert _asset_at(repo, "REUSE", handover - timedelta(seconds=1)) == old.id
    assert _asset_at(repo, "REUSE", handover) == new.id
    assert repo.resolve_many("alpaca", ["REUSE", "UNMAPPED"], T0) == {"REUSE": old.id}
    assert [m.status for m in repo.history("alpaca", "REUSE")] == [
        MappingStatus.RETIRED,
        MappingStatus.ACTIVE,
    ]


def test_overlapping_symbol_periods_are_rejected(session):
    repo = AssetMappingRepository(session)
    a, b = _asset(session, "OVLA"), _asset(session, "OVLB")
    first = repo.register(asset_id=a.id, provider="alpaca", provider_symbol="OVL", valid_from=T0)
    repo.retire(first.id, T0 + timedelta(days=10))

    # Starts before the first mapping ended: would make OVL ambiguous on days 5-10.
    with pytest.raises(IntegrityError), session.begin_nested():
        repo.register(
            asset_id=b.id,
            provider="alpaca",
            provider_symbol="OVL",
            valid_from=T0 + timedelta(days=5),
        )


def test_an_asset_has_one_symbol_per_provider_at_a_time(session):
    repo = AssetMappingRepository(session)
    asset = _asset(session, "ONEX")
    repo.register(asset_id=asset.id, provider="alpaca", provider_symbol="ONEX", valid_from=T0)

    with pytest.raises(IntegrityError), session.begin_nested():
        repo.register(
            asset_id=asset.id,
            provider="alpaca",
            provider_symbol="ONEX2",
            valid_from=T0 + timedelta(days=1),
        )


def test_status_must_agree_with_validity(session):
    asset = _asset(session, "STAT")
    _rejects(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol, valid_to) "
        "VALUES (:id, 'alpaca', 'STAT', now() + interval '1 day')",
        {"id": asset.id},
    )
    _rejects(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol, status) "
        "VALUES (:id, 'alpaca', 'STAT', 'RETIRED')",
        {"id": asset.id},
    )
    _rejects(
        session,
        "INSERT INTO asset_identifiers (asset_id, provider, provider_symbol, status) "
        "VALUES (:id, 'alpaca', 'STAT', 'DELETED')",
        {"id": asset.id},
    )


def test_retire_validation(session):
    repo = AssetMappingRepository(session)
    asset = _asset(session, "RETX")
    mapping = repo.register(
        asset_id=asset.id, provider="alpaca", provider_symbol="RETX", valid_from=T0
    )

    with pytest.raises(MappingError, match="after valid_from"):
        repo.retire(mapping.id, T0)
    repo.retire(mapping.id, T0 + timedelta(days=1))
    with pytest.raises(MappingError, match="already retired"):
        repo.retire(mapping.id, T0 + timedelta(days=2))


def test_register_rejects_naive_timestamps_and_blank_symbols(session):
    repo = AssetMappingRepository(session)
    asset = _asset(session, "NAIV")
    with pytest.raises(ValueError, match="naive"):
        repo.register(
            asset_id=asset.id,
            provider="alpaca",
            provider_symbol="NAIV",
            valid_from=datetime(2020, 1, 1),  # noqa: DTZ001
        )
    with pytest.raises(MappingError):
        repo.register(asset_id=asset.id, provider="alpaca", provider_symbol=" ", valid_from=T0)


def test_mapped_assets_cannot_be_deleted(session):
    repo = AssetMappingRepository(session)
    asset = _asset(session, "KEEP")
    repo.register(asset_id=asset.id, provider="alpaca", provider_symbol="KEEP", valid_from=T0)
    _rejects(session, "DELETE FROM assets WHERE id = :id", {"id": asset.id}, error=DBAPIError)

"""Directory discovery against real PostgreSQL (TEST FIXTURE directory, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.asset_discovery import membership
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    DiscoveryOutcome,
    LifecycleEventType,
    ProviderAssetStatus,
    UniverseLayer,
    ValidFromBasis,
)
from app.asset_discovery.sessions import CALENDARS
from app.data_engine.providers.alpaca.reference import REFERENCE_CAPABILITIES
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.database.models import (
    Asset,
    AssetIdentifier,
    AssetLifecycleEventRecord,
    AssetReferenceVersionRecord,
    AssetStatus,
    DiscoveryResultRecord,
    ExchangeRecord,
    ProviderAssetRecord,
    ProviderExchangeCodeRecord,
)
from app.database.repositories import AssetMappingRepository
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca_assets import asset_record, fixture_id
from app.tests.integration.asset_discovery_support import T0, load_asset, make_env

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    return make_env(fresh_engine)


def lifecycle(env, symbol_or_id) -> list[LifecycleEventType]:
    with session_scope(env.factory) as session:
        asset_id = (
            symbol_or_id
            if not isinstance(symbol_or_id, str)
            else env.asset_id(symbol_or_id, active_only=False)
        )
        return list(
            session.scalars(
                select(AssetLifecycleEventRecord.event_type)
                .where(AssetLifecycleEventRecord.asset_id == asset_id)
                .order_by(AssetLifecycleEventRecord.id)
            )
        )


def mappings(env, asset_id) -> list[AssetIdentifier]:
    with session_scope(env.factory) as session:
        rows = session.scalars(
            select(AssetIdentifier)
            .where(AssetIdentifier.asset_id == asset_id)
            .order_by(AssetIdentifier.valid_from)
        ).all()
        for r in rows:
            session.expunge(r)
        return list(rows)


def in_pool(env, asset_id) -> bool:
    with session_scope(env.factory) as session:
        state = membership.current_states(session, UniverseLayer.DISCOVERY_POOL, [asset_id])
        return bool(state.get(asset_id) and state[asset_id].member)


def test_initial_discovery_catalogs_identity_versions_mappings_and_pool(env):
    env.directory.set(
        asset_record("AAA"),
        asset_record("BBB", exchange="NYSE"),
        asset_record("GONE", status="inactive", tradable=False),
        asset_record("DEAD_DELISTED", key="dead", status="inactive", tradable=False),
    )
    job = env.discover()
    assert job.status is DiscoveryJobStatus.COMPLETED
    assert job.counters["new"] == 4
    with session_scope(env.factory) as session:
        assets = {a.symbol: a for a in session.scalars(select(Asset))}
        assert assets["AAA"].status is AssetStatus.ACTIVE
        assert assets["AAA"].exchange_code == "XNAS"
        assert assets["BBB"].exchange_code == "XNYS"
        assert assets["GONE"].status is AssetStatus.INACTIVE
        assert assets["DEAD"].status is AssetStatus.DELISTED
        assert assets["AAA"].first_discovered_at == T0
        assert assets["AAA"].metadata_source == "alpaca:reference-directory"
        identities = session.scalars(select(ProviderAssetRecord)).all()
        assert {p.provider_asset_id for p in identities} == {
            fixture_id(k) for k in ("AAA", "BBB", "GONE", "dead")
        }
        version = session.scalars(
            select(AssetReferenceVersionRecord).where(AssetReferenceVersionRecord.symbol == "AAA")
        ).one()
        assert version.version == 1
        assert version.tradable is True
        assert '"symbol":"AAA"' in version.raw_json
    aaa = mappings(env, env.asset_id("AAA"))
    assert len(aaa) == 1
    assert aaa[0].valid_from_basis is ValidFromBasis.PROVIDER_HISTORY_ASSUMED
    assert aaa[0].valid_from == datetime(2016, 1, 1, tzinfo=UTC)
    # Inactive instruments: catalogued, never mapped, never in the pool.
    gone = env.asset_id("GONE", active_only=False)
    assert mappings(env, gone) == []
    assert not in_pool(env, gone)
    assert in_pool(env, env.asset_id("AAA"))
    assert lifecycle(env, "AAA") == [LifecycleEventType.FIRST_DISCOVERED]
    assert lifecycle(env, "DEAD") == [
        LifecycleEventType.FIRST_DISCOVERED,
        LifecycleEventType.DELISTED,
    ]


def test_repeated_discovery_is_idempotent(env):
    env.directory.set(asset_record("AAA"), asset_record("BBB", status="inactive", tradable=False))
    env.discover()
    before = env.counts()
    env.clock.advance(timedelta(hours=1))
    job = env.discover()
    assert job.counters["unchanged"] == 2
    assert job.counters["new"] == 0
    assert env.counts() == before
    with session_scope(env.factory) as session:
        row = session.scalars(
            select(ProviderAssetRecord).where(ProviderAssetRecord.current_symbol == "AAA")
        ).one()
        assert row.last_seen_at == T0 + timedelta(hours=1)
        assert load_asset(session, row.asset_id).last_verified_at == T0 + timedelta(hours=1)


def test_operator_registered_asset_is_adopted_not_duplicated(env):
    with session_scope(env.factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    aapl = env.asset_id("AAPL")
    env.directory.set(asset_record("AAPL"), asset_record("SPY", exchange="ARCA"))
    env.discover()
    with session_scope(env.factory) as session:
        assert session.scalar(select(text("count(*)")).select_from(Asset)) == 2
        assert load_asset(session, aapl).first_discovered_at == T0
    kept = mappings(env, aapl)
    assert len(kept) == 1
    assert kept[0].valid_from_basis is ValidFromBasis.OPERATOR_REFERENCE  # untouched
    assert in_pool(env, aapl)


def test_symbol_change_keeps_identity_and_point_in_time_mappings(env):
    env.directory.set(asset_record("OLDN", key="renamed"))
    env.discover()
    asset_id = env.asset_id("OLDN")
    env.clock.advance(timedelta(days=3))
    renamed_at = env.clock.now()
    env.directory.set(asset_record("NEWN", key="renamed"))
    job = env.discover()
    assert job.counters["changed"] == 1
    assert env.asset_id("NEWN") == asset_id  # same instrument, same Atlas ID
    periods = mappings(env, asset_id)
    assert [(m.provider_symbol, m.valid_to) for m in periods] == [
        ("OLDN", renamed_at),
        ("NEWN", None),
    ]
    assert periods[1].valid_from_basis is ValidFromBasis.OBSERVED
    with session_scope(env.factory) as session:
        repo = AssetMappingRepository(session)
        mapping = repo.resolve("alpaca", "OLDN", T0 + timedelta(days=1))
        assert mapping is not None
        assert mapping.asset_id == asset_id
        assert repo.resolve("alpaca", "OLDN", renamed_at + timedelta(seconds=1)) is None
    assert LifecycleEventType.SYMBOL_CHANGE in lifecycle(env, asset_id)


def test_symbol_reuse_creates_a_distinct_instrument(env):
    # Alpaca lists a retired instrument and its successor under the same symbol.
    env.directory.set(
        asset_record("REUSE", key="old-reuse", status="inactive", tradable=False),
        asset_record("REUSE", key="new-reuse"),
    )
    env.discover()
    with session_scope(env.factory) as session:
        rows = session.scalars(select(Asset).where(Asset.symbol == "REUSE")).all()
        assert sorted(a.status.value for a in rows) == ["ACTIVE", "INACTIVE"]
    new = env.asset_id("REUSE")
    (mapping,) = mappings(env, new)
    # Another known instrument used the symbol, so history cannot be assumed.
    assert mapping.valid_from_basis is ValidFromBasis.OBSERVED
    assert mapping.valid_from == T0


def test_successor_takes_over_a_symbol_freed_in_the_same_listing(env):
    env.directory.set(asset_record("TAKE", key="first"))
    env.discover()
    first = env.asset_id("TAKE")
    env.clock.advance(timedelta(days=1))
    env.directory.set(
        asset_record("TAKE", key="first", status="inactive", tradable=False),
        asset_record("TAKE", key="second"),
    )
    job = env.discover()
    assert job.counters["new"] == 1
    assert job.counters["conflicts"] == 0
    second = env.asset_id("TAKE")
    assert second != first
    assert mappings(env, first)[0].valid_to == env.clock.now()
    assert mappings(env, second)[0].valid_from == env.clock.now()


def test_deactivation_delisting_and_reactivation_lifecycle(env):
    env.directory.set(asset_record("LIFE", key="life"))
    env.discover()
    asset_id = env.asset_id("LIFE")
    env.clock.advance(timedelta(days=1))
    env.directory.set(asset_record("LIFE", key="life", status="inactive", tradable=False))
    env.discover()
    with session_scope(env.factory) as session:
        assert load_asset(session, asset_id).status is AssetStatus.INACTIVE
    assert not in_pool(env, asset_id)
    assert mappings(env, asset_id)[0].valid_to == env.clock.now()
    env.clock.advance(timedelta(days=1))
    env.directory.set(asset_record("LIFE_DELISTED", key="life", status="inactive", tradable=False))
    env.discover()
    with session_scope(env.factory) as session:
        assert load_asset(session, asset_id).status is AssetStatus.DELISTED
    env.clock.advance(timedelta(days=1))
    env.directory.set(asset_record("LIFE", key="life"))
    env.discover()
    with session_scope(env.factory) as session:
        assert load_asset(session, asset_id).status is AssetStatus.ACTIVE
    assert in_pool(env, asset_id)
    events = lifecycle(env, asset_id)
    assert events[:3] == [
        LifecycleEventType.FIRST_DISCOVERED,
        LifecycleEventType.DEACTIVATED,
        LifecycleEventType.DELISTED,
    ]
    assert LifecycleEventType.REACTIVATED in events
    assert [m.valid_to is None for m in mappings(env, asset_id)] == [False, True]
    with session_scope(env.factory) as session:
        history = [
            e.action.value
            for e in membership.history(session, asset_id, UniverseLayer.DISCOVERY_POOL)
        ]
    assert history == ["ADDED", "REMOVED", "ADDED"]


def test_exchange_name_tradability_and_metadata_changes_are_events(env):
    env.directory.set(asset_record("CHG", key="chg"))
    env.discover()
    env.clock.advance(timedelta(days=1))
    env.directory.set(
        asset_record(
            "CHG",
            key="chg",
            exchange="NYSE",
            name="Renamed Fixture Co (test data)",
            tradable=False,
            shortable=False,
            easy_to_borrow=False,
        )
    )
    job = env.discover()
    events = lifecycle(env, "CHG")
    for expected in (
        LifecycleEventType.EXCHANGE_CHANGE,
        LifecycleEventType.NAME_CHANGE,
        LifecycleEventType.TRADING_SUSPENDED,
        LifecycleEventType.METADATA_CHANGE,
    ):
        assert expected in events
    with session_scope(env.factory) as session:
        asset = load_asset(session, env.asset_id("CHG"))
        assert asset.exchange_code == "XNYS"
        versions = session.scalars(
            select(AssetReferenceVersionRecord.version)
            .where(AssetReferenceVersionRecord.asset_id == asset.id)
            .order_by(AssetReferenceVersionRecord.version)
        ).all()
        assert versions == [1, 2]
        result = session.scalars(
            select(DiscoveryResultRecord).where(DiscoveryResultRecord.job_id == job.id)
        ).one()
        assert result.outcome is DiscoveryOutcome.CHANGED


def test_absence_from_full_listing_is_recorded_not_interpreted(env):
    env.directory.set(asset_record("STAY"), asset_record("VANISH"))
    env.discover()
    vanish = env.asset_id("VANISH")
    env.clock.advance(timedelta(days=1))
    env.directory.set(asset_record("STAY"))
    job = env.discover()
    assert job.counters["missing"] == 1
    with session_scope(env.factory) as session:
        assert load_asset(session, vanish).status is AssetStatus.ACTIVE  # not assumed delisted
    assert in_pool(env, vanish)
    assert lifecycle(env, vanish)[-1] is LifecycleEventType.MISSING_FROM_DIRECTORY
    env.clock.advance(timedelta(days=1))
    env.directory.set(asset_record("STAY"), asset_record("VANISH"))
    env.discover()
    assert lifecycle(env, vanish)[-1] is LifecycleEventType.REAPPEARED_IN_DIRECTORY


def test_incremental_listing_verifies_absent_active_instruments(env):
    env.directory.set(asset_record("KEEP"), asset_record("DROP"), asset_record("LOST"))
    env.discover()
    env.clock.advance(timedelta(days=1))
    env.directory.set(
        asset_record("KEEP"),
        asset_record("DROP", status="inactive", tradable=False),
        asset_record("NEWLY"),
    )
    job = env.discover(mode="INCREMENTAL")
    assert job.status is DiscoveryJobStatus.COMPLETED
    listed = env.directory.list_requests()[-1]
    assert listed.url.params["status"] == "active"
    assert job.counters["new"] == 1
    assert job.counters["verified"] == 1  # DROP looked up by its ID
    assert job.counters["missing"] == 1  # LOST: provider answers not found
    assert LifecycleEventType.NEW_LISTING in lifecycle(env, "NEWLY")
    assert LifecycleEventType.DEACTIVATED in lifecycle(env, "DROP")


def test_verification_is_bounded(fresh_engine):
    env = make_env(fresh_engine, verify_missing_max=1)
    env.directory.set(asset_record("A1"), asset_record("A2"), asset_record("A3"))
    env.discover()
    env.directory.set()
    job = env.discover(mode="INCREMENTAL")
    assert job.counters["verification_deferred"] == 2
    assert job.status is DiscoveryJobStatus.PARTIALLY_COMPLETED


def test_malformed_records_partially_complete_the_job(env):
    env.directory.set(
        asset_record("GOOD"),
        asset_record("BAD") | {"tradable": "sometimes"},
        {"symbol": "NOID"},
    )
    job = env.discover()
    assert job.status is DiscoveryJobStatus.PARTIALLY_COMPLETED
    assert job.counters["malformed"] == 2
    with session_scope(env.factory) as session:
        rejected = session.scalars(
            select(DiscoveryResultRecord).where(
                DiscoveryResultRecord.outcome == DiscoveryOutcome.REJECTED
            )
        ).all()
        assert {r.provider_symbol for r in rejected} == {"BAD", "NOID"}
        assert session.scalars(select(Asset.symbol)).all() == ["GOOD"]
    # A later listing where BAD is still malformed does not mark it missing.
    env.clock.advance(timedelta(days=1))
    assert env.discover().counters["missing"] == 0


def test_provider_outage_requeues_then_fails(env):
    env.directory.set(asset_record("AAA"))
    env.directory.fail_with = [503] * 3
    job = env.discover()
    assert job.status is DiscoveryJobStatus.PENDING  # re-queued with backoff
    assert job.attempt == 1
    assert job.recent_errors[-1]["code"] == "provider_unavailable"
    assert job.next_attempt_at > env.clock.now()
    env.clock.advance(timedelta(minutes=5))
    asyncio_run_due(env)
    assert env.job(job.id).status is DiscoveryJobStatus.COMPLETED


def test_attempts_are_bounded(fresh_engine):
    env = make_env(fresh_engine, job_max_attempts=2)
    env.directory.set(asset_record("AAA"))
    env.directory.fail_with = [503] * 6
    job = env.discover()
    env.clock.advance(timedelta(hours=2))
    asyncio_run_due(env)
    final = env.job(job.id)
    assert final.status is DiscoveryJobStatus.FAILED
    assert final.attempt == 2
    assert len(final.recent_errors) == 2


def test_rejected_credentials_fail_permanently(env):
    env.directory.fail_with = [401]
    job = env.discover()
    assert job.status is DiscoveryJobStatus.FAILED
    assert job.recent_errors[0]["code"] == "provider_authentication_failed"


def test_unmapped_exchange_is_kept_without_a_mic(env):
    env.directory.set(asset_record("TXCO", exchange="TXSE"), asset_record("OTCC", exchange="OTC"))
    job = env.discover()
    assert job.counters["unmapped_exchange"] == 1
    with session_scope(env.factory) as session:
        assets = {a.symbol: a.exchange_code for a in session.scalars(select(Asset))}
    assert assets == {"TXCO": None, "OTCC": "OTCM"}


def test_outside_trading_hours_nothing_becomes_inactive(fresh_engine):
    saturday = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    env = make_env(fresh_engine, now=saturday)
    env.directory.set(asset_record("WKND"))
    env.discover()
    env.clock.advance(timedelta(days=1))  # Sunday
    env.discover()
    with session_scope(env.factory) as session:
        assert load_asset(session, env.asset_id("WKND")).status is AssetStatus.ACTIVE
    assert lifecycle(env, "WKND") == [LifecycleEventType.FIRST_DISCOVERED]


def test_append_only_tables_reject_changes(env):
    env.directory.set(asset_record("IMM"))
    env.discover()
    with env.factory() as session:
        for statement in (
            "UPDATE asset_reference_versions SET tradable = false",
            "DELETE FROM asset_lifecycle_events",
            "UPDATE universe_membership_events SET reason = 'x'",
            "TRUNCATE universe_membership_events CASCADE",
        ):
            with pytest.raises(DBAPIError):
                session.execute(text(statement))
            session.rollback()


def test_reference_seed_data_is_consistent_with_the_code(env):
    with session_scope(env.factory) as session:
        venues = session.scalars(select(ExchangeRecord)).all()
        for venue in venues:
            assert venue.calendar_code is None or venue.calendar_code in CALENDARS
        codes = {
            r.provider_exchange
            for r in session.scalars(
                select(ProviderExchangeCodeRecord).where(
                    ProviderExchangeCodeRecord.provider == "alpaca"
                )
            )
        }
    assert set(REFERENCE_CAPABILITIES.exchanges) <= codes


def test_metadata_refresh_reads_tracked_assets_one_by_one(env):
    env.directory.set(asset_record("R1"), asset_record("R2"))
    env.discover()
    env.clock.advance(timedelta(hours=1))
    env.directory.set(asset_record("R1", shortable=False), asset_record("R2"))
    before = len(env.directory.requests)
    job = env.run(DiscoveryJobKind.METADATA_REFRESH, max_assets=5)
    assert job.status is DiscoveryJobStatus.COMPLETED
    lookups = [r.url.path for r in env.directory.requests[before:]]
    assert all(p.startswith("/v2/assets/") for p in lookups)
    assert len(lookups) == 2
    assert job.counters["changed"] == 1
    with session_scope(env.factory) as session:
        row = session.scalars(
            select(ProviderAssetRecord).where(ProviderAssetRecord.current_symbol == "R1")
        ).one()
        assert row.current_version == 2
        assert row.provider_status is ProviderAssetStatus.ACTIVE


def asyncio_run_due(env) -> None:
    import asyncio

    asyncio.run(env.service.run_due())

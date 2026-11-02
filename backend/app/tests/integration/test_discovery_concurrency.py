"""Concurrency and failure recovery of discovery jobs (real PostgreSQL, fixture directory).

Threads use separate sessions and connections, as separate workers would.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.asset_discovery import jobs, membership
from app.asset_discovery.discovery import DirectoryCounters
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    JobTrigger,
    ReferenceSource,
    UniverseLayer,
)
from app.data_engine.reference import InstrumentDirectoryRequest
from app.database.models import (
    Asset,
    AssetClass,
    AssetReferenceVersionRecord,
    DiscoveryJobRecord,
    ProviderAssetRecord,
    UniverseMembershipEventRecord,
)
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca_assets import asset_record
from app.tests.integration.asset_discovery_support import make_env, seed_daily_bars

pytestmark = pytest.mark.integration

RECORDS = [asset_record(f"C{i:02d}") for i in range(40)] + [
    asset_record(f"I{i:02d}", status="inactive", tradable=False) for i in range(10)
]


@pytest.fixture
def env(fresh_engine):
    env = make_env(fresh_engine)
    env.directory.set(*RECORDS)
    return env


def parallel(*targets) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(targets))

    def wrap(target):
        def run():
            barrier.wait()
            try:
                target()
            except BaseException as exc:  # collected and asserted by the caller
                errors.append(exc)

        return run

    threads = [threading.Thread(target=wrap(t)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return errors


def job_row(env, kind=DiscoveryJobKind.DIRECTORY_DISCOVERY, status=DiscoveryJobStatus.COMPLETED):
    """A finished job row to attach writes to (direct engine tests)."""
    with session_scope(env.factory) as session:
        job = DiscoveryJobRecord(
            id=uuid.uuid4(),
            kind=kind,
            provider="alpaca",
            status=status,
            trigger=JobTrigger.CLI,
            requested_by="test",
            params={},
            attempt=1,
            max_attempts=1,
            next_attempt_at=env.clock.now(),
            created_at=env.clock.now(),
            completed_at=env.clock.now(),
            counters={},
            summary={},
            recent_errors=[],
        )
        session.add(job)
    return job.id


def test_duplicate_delivery_of_the_same_directory_never_duplicates(env):
    listing = asyncio.run(
        env.service.engine._provider.list_instruments(
            InstrumentDirectoryRequest(asset_class=AssetClass.EQUITY)
        )
    )
    first, second = job_row(env), job_row(env)

    def apply(job_id):
        return lambda: env.service.engine.apply_directory(
            job_id, listing, ReferenceSource.DIRECTORY_FULL, DirectoryCounters()
        )

    assert parallel(apply(first), apply(second)) == []
    counts = env.counts()
    assert counts["assets"] == 50
    assert counts["provider_assets"] == 50
    assert counts["asset_reference_versions"] == 50
    assert counts["universe_membership_events"] == 40  # one pool addition per active asset
    assert counts["asset_identifiers"] == 40


def test_concurrent_job_requests_create_one_job(env):
    results = []

    def request():
        results.append(
            env.service.request(
                DiscoveryJobKind.DIRECTORY_DISCOVERY,
                trigger=JobTrigger.API,
                requested_by="t",
                params={"mode": "FULL"},
            )
        )

    assert parallel(*[request] * 6) == []
    assert sum(r.created for r in results) == 1
    assert len({r.job.id for r in results}) == 1


def test_concurrent_claims_run_a_job_once(env):
    created = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.CLI,
        requested_by="t",
        params={"mode": "FULL"},
    )
    claimed: list[object] = []

    def claim():
        with session_scope(env.factory) as session:
            job = jobs.claim_next(
                session,
                owner=f"w-{threading.get_ident()}",
                now=env.clock.now(),
                lease=timedelta(minutes=5),
            )
            if job is not None:
                claimed.append(job.id)

    assert parallel(*[claim] * 5) == []
    assert claimed == [created.job.id]


def test_discovery_and_metadata_refresh_running_together_stay_consistent(env):
    env.discover()
    env.clock.advance(timedelta(hours=1))
    env.directory.set(*[r | {"shortable": False} for r in RECORDS])
    errors = parallel(
        lambda: env.discover(),
        lambda: env.run(DiscoveryJobKind.METADATA_REFRESH, max_assets=40),
    )
    assert errors == []
    with session_scope(env.factory) as session:
        versions = session.execute(
            select(AssetReferenceVersionRecord.provider_asset_ref, func.count()).group_by(
                AssetReferenceVersionRecord.provider_asset_ref
            )
        ).all()
        # Every instrument changed once: version 2 exists exactly once, never twice.
        assert {count for _, count in versions} == {2}
        latest = session.scalars(select(ProviderAssetRecord.current_version)).all()
        assert set(latest) == {2}


def test_concurrent_membership_updates_keep_one_consistent_chain(env):
    env.discover()
    env.clock.advance(timedelta(minutes=5))
    for i in range(5):
        seed_daily_bars(env, f"C{i:02d}", close="40", volume=500_000)
    qualification = env.run(DiscoveryJobKind.QUALIFICATION, prepare_history=False)
    decisions = env.service._membership_decisions(
        UniverseLayer.QUALIFIED_UNIVERSE, qualification.id
    )
    update_a = job_row(env, DiscoveryJobKind.MEMBERSHIP_UPDATE)
    update_b = job_row(env, DiscoveryJobKind.MEMBERSHIP_UPDATE)

    def apply(job_id):
        def run():
            for attempt in range(3):
                try:
                    with session_scope(env.factory) as session:
                        env.service._apply_decisions(
                            session, job_id, UniverseLayer.QUALIFIED_UNIVERSE, decisions
                        )
                    return
                except Exception:
                    if attempt == 2:
                        raise

        return run

    assert parallel(apply(update_a), apply(update_b)) == []
    with session_scope(env.factory) as session:
        added = session.scalar(
            select(func.count())
            .select_from(UniverseMembershipEventRecord)
            .where(UniverseMembershipEventRecord.layer == UniverseLayer.QUALIFIED_UNIVERSE)
        )
        assert added == 5  # each asset added exactly once despite two writers
        assert len(membership.current_members(session, UniverseLayer.QUALIFIED_UNIVERSE)) == 5


def test_worker_crash_is_recovered_and_the_rerun_is_idempotent(env):
    created = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.CLI,
        requested_by="t",
        params={"mode": "FULL"},
    )
    with session_scope(env.factory) as session:
        jobs.claim_next(
            session, owner="dead-worker", now=env.clock.now(), lease=timedelta(minutes=1)
        )
    # The dead worker applied part of the directory before it died.
    listing = asyncio.run(
        env.service.engine._provider.list_instruments(
            InstrumentDirectoryRequest(asset_class=AssetClass.EQUITY)
        )
    )
    partial = listing.model_copy(update={"instruments": listing.instruments[:15]})
    env.service.engine.apply_directory(
        created.job.id, partial, ReferenceSource.DIRECTORY_FULL, DirectoryCounters()
    )
    assert env.service.recover() == 0  # lease still valid
    env.clock.advance(timedelta(minutes=2))
    assert env.service.recover() == 1
    recovered = env.job(created.job.id)
    assert recovered.status is DiscoveryJobStatus.PENDING
    assert recovered.recent_errors[-1]["code"] == "worker_lost"
    asyncio.run(env.service.run_due(job_id=created.job.id))
    assert env.job(created.job.id).status is DiscoveryJobStatus.COMPLETED
    counts = env.counts()
    assert counts["assets"] == counts["provider_assets"] == 50
    assert counts["asset_reference_versions"] == 50


def test_database_interruption_mid_job_requeues_without_partial_duplicates(env, monkeypatch):
    engine = env.service.engine
    original = engine._apply_chunk
    calls = {"n": 0}

    def flaky(ctx, instruments, counters):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return original(ctx, instruments, counters)

    monkeypatch.setattr(engine, "_apply_chunk", flaky)
    job = env.discover()
    assert job.status is DiscoveryJobStatus.PENDING
    assert job.recent_errors[-1]["message"].startswith("database error")
    env.clock.advance(timedelta(minutes=5))
    asyncio.run(env.service.run_due(job_id=job.id))
    assert env.job(job.id).status is DiscoveryJobStatus.COMPLETED
    counts = env.counts()
    assert counts["assets"] == 50
    assert counts["universe_membership_events"] == 40


def test_repeated_requests_while_running_return_the_active_job(env):
    first = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.API,
        requested_by="t",
        params={"mode": "FULL"},
    )
    second = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.API,
        requested_by="t",
        params={"mode": "INCREMENTAL"},
    )
    assert not second.created
    assert second.job.id == first.job.id
    asyncio.run(env.service.run_due())
    third = env.service.request(
        DiscoveryJobKind.DIRECTORY_DISCOVERY,
        trigger=JobTrigger.API,
        requested_by="t",
        params={"mode": "FULL"},
    )
    assert third.created  # the first one finished


def test_concurrent_qualification_evaluations_write_separately(env):
    env.discover()
    env.clock.advance(timedelta(minutes=5))
    seed_daily_bars(env, "C00", close="40", volume=500_000)
    a, b = (
        job_row(env, DiscoveryJobKind.QUALIFICATION),
        job_row(env, DiscoveryJobKind.QUALIFICATION),
    )
    now = env.clock.now()
    errors = parallel(
        lambda: env.service.qualification.evaluate_all(a, now, live=True),
        lambda: env.service.qualification.evaluate_all(b, now, live=True),
    )
    assert errors == []
    from app.database.models import QualificationEvaluationRecord as E

    with session_scope(env.factory) as session:
        per_job = dict(session.execute(select(E.job_id, func.count()).group_by(E.job_id)).all())
        assert per_job[a] == per_job[b] == 40
        assert session.scalar(select(func.count()).select_from(Asset)) == 50

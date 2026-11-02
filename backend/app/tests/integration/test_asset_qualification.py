"""Qualification, scoring and point-in-time membership against real PostgreSQL.

Instruments come from the TEST FIXTURE directory; bars and quotes are MOCK observations
inserted with controlled ingestion times (see ``asset_discovery_support``).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.asset_discovery import membership
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    MembershipAction,
    MetricStatus,
    QualificationOutcome,
    UniverseLayer,
)
from app.asset_discovery.metrics import FEATURE_VERSION
from app.asset_discovery.qualification import QualificationService
from app.asset_discovery.rules import DEFAULT_RULE_SETS
from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.schemas import DataIngestionJob
from app.database.models import (
    AssetMetricValueRecord,
    AuditLog,
    QualificationEvaluationRecord,
    QualificationRuleSetRecord,
    WatchlistScoreRecord,
)
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca_assets import asset_record
from app.tests.integration.asset_discovery_support import (
    T0,
    load_asset,
    make_env,
    seed_daily_bars,
    seed_quotes,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    env = make_env(fresh_engine)
    env.directory.set(
        asset_record("LIQ"),
        asset_record("LIQ2", exchange="NYSE"),
        asset_record("CHEAP"),
        asset_record("THIN"),
        asset_record("NOHIST"),
        asset_record("OTCX", exchange="OTC"),
        asset_record("NOTRADE", tradable=False),
    )
    env.discover()
    env.clock.advance(timedelta(minutes=5))
    seed_daily_bars(env, "LIQ", close="40", volume=500_000)
    seed_daily_bars(env, "LIQ2", close="60", volume=300_000)
    seed_daily_bars(env, "CHEAP", close="2", volume=900_000)
    seed_daily_bars(env, "THIN", close="30", volume=1_000)
    return env


def evaluations(env, job_id) -> dict[str, QualificationEvaluationRecord]:
    with session_scope(env.factory) as session:
        rows = session.scalars(
            select(QualificationEvaluationRecord).where(
                QualificationEvaluationRecord.job_id == job_id
            )
        ).all()
        symbols = {env_symbol(env, r.asset_id): r for r in rows}
        for r in rows:
            session.expunge(r)
        return symbols


def env_symbol(env, asset_id) -> str:
    with session_scope(env.factory) as session:
        return load_asset(session, asset_id).symbol


def members(env, layer: UniverseLayer, at=None, known_at=None) -> set[str]:
    with session_scope(env.factory) as session:
        found = (
            membership.current_members(session, layer)
            if at is None
            else membership.members_at(session, layer, at, known_at=known_at)
        )
    return {env_symbol(env, a) for a in found}


def pipeline(env) -> dict[DiscoveryJobKind | str, Any]:
    job = env.run(
        DiscoveryJobKind.QUALIFICATION, pipeline=True, pipeline_step=0, prepare_history=False
    )
    assert job.status is DiscoveryJobStatus.COMPLETED, job.error
    return {
        kind: env.latest(kind)
        for kind in (DiscoveryJobKind.QUALIFICATION, DiscoveryJobKind.WATCHLIST_SCORING)
    } | {"job": job}


def test_qualification_pipeline_end_to_end(env):
    jobs = pipeline(env)
    found = evaluations(env, jobs[DiscoveryJobKind.QUALIFICATION].id)
    assert found["LIQ"].outcome is QualificationOutcome.QUALIFIED
    assert found["LIQ2"].outcome is QualificationOutcome.QUALIFIED
    assert found["CHEAP"].outcome is QualificationOutcome.DISQUALIFIED
    assert "min_price" in found["CHEAP"].failed_rules
    assert "min_avg_daily_volume" in found["THIN"].failed_rules
    assert found["NOHIST"].outcome is QualificationOutcome.INSUFFICIENT_DATA
    assert "history_coverage" in found["NOHIST"].unavailable_rules
    assert found["OTCX"].failed_rules[:1] == ["supported_exchange"]
    assert "tradable" in found["NOTRADE"].failed_rules
    # Short-eligibility flags are carried as reference data, with the revalidation caveat.
    assert found["LIQ"].short_eligibility["shortable"] is True
    assert "revalidated" in found["LIQ"].short_eligibility["note"]

    assert members(env, UniverseLayer.QUALIFIED_UNIVERSE) == {"LIQ", "LIQ2"}
    assert members(env, UniverseLayer.ACTIVE_WATCHLIST) == {"LIQ", "LIQ2"}
    with session_scope(env.factory) as session:
        scores = session.scalars(
            select(WatchlistScoreRecord)
            .where(WatchlistScoreRecord.job_id == jobs[DiscoveryJobKind.WATCHLIST_SCORING].id)
            .order_by(WatchlistScoreRecord.rank)
        ).all()
        assert [s.rank for s in scores] == [1, 2]
        assert all("not a trading signal" in s.explanation for s in scores)
        assert set(scores[0].factor_scores) >= {"liquidity", "data_reliability"}
        assert scores[0].factor_scores["spread_quality"]["available"] is False


def test_measurements_record_window_source_range_and_version(env):
    jobs = pipeline(env)
    with session_scope(env.factory) as session:
        rows = {
            m.metric: m
            for m in session.scalars(
                select(AssetMetricValueRecord).where(
                    AssetMetricValueRecord.job_id == jobs[DiscoveryJobKind.QUALIFICATION].id,
                    AssetMetricValueRecord.asset_id == env.asset_id("LIQ"),
                )
            )
        }
    adv = rows["avg_daily_volume"]
    assert adv.status is MetricStatus.OK
    assert adv.value is not None
    assert float(adv.value) == pytest.approx(500_000)
    assert adv.window == "20 sessions"
    assert adv.feature_version == FEATURE_VERSION
    assert adv.source_start is not None
    assert adv.source_end is not None
    assert adv.source_end <= adv.as_of
    assert rows["median_spread_bps"].status is MetricStatus.NO_DATA
    assert rows["session_status_counts"].details["counts"] == {"TRADED": 60}
    no_rows = session_scope(env.factory)
    with no_rows as session:
        assert not session.scalars(
            select(AssetMetricValueRecord).where(
                AssetMetricValueRecord.asset_id == env.asset_id("NOHIST")
            )
        ).first()


def test_spread_is_applied_when_quotes_exist(env):
    seed_quotes(env, "LIQ2", as_of=T0, count=600, bid="59.00", ask="60.00")  # ~168 bps: wide
    jobs = pipeline(env)
    found = evaluations(env, jobs[DiscoveryJobKind.QUALIFICATION].id)
    spread = next(r for r in found["LIQ2"].rule_results if r["rule"] == "max_spread")
    assert spread["status"] == "FAIL"
    assert found["LIQ2"].outcome is QualificationOutcome.QUALIFIED  # optional rule


def test_bars_ingested_after_as_of_are_not_used(env):
    late = env.clock.now() + timedelta(days=1)
    seed_daily_bars(env, "NOHIST", close="40", volume=500_000, ingested_at=late)
    early = env.run(DiscoveryJobKind.QUALIFICATION, prepare_history=False)
    assert evaluations(env, early.id)["NOHIST"].outcome is QualificationOutcome.INSUFFICIENT_DATA
    env.clock.set(late + timedelta(hours=1))
    later = env.run(DiscoveryJobKind.QUALIFICATION, prepare_history=False)
    assert evaluations(env, later.id)["NOHIST"].outcome is QualificationOutcome.QUALIFIED


def test_historical_evaluation_uses_metadata_known_then(env):
    before_change = env.clock.now()
    env.clock.advance(timedelta(days=1))
    env.directory.set(
        *[r if r["symbol"] != "LIQ" else r | {"tradable": False} for r in env.directory.records]
    )
    env.discover()
    now_job = env.run(DiscoveryJobKind.QUALIFICATION, prepare_history=False)
    assert "tradable" in evaluations(env, now_job.id)["LIQ"].failed_rules
    past = env.run(DiscoveryJobKind.QUALIFICATION, as_of=before_change.isoformat())
    past_eval = evaluations(env, past.id)["LIQ"]
    assert past_eval.outcome is QualificationOutcome.QUALIFIED  # tradable as known then
    assert past.summary["live"] is False


def test_historical_evaluation_never_changes_membership(env):
    before = env.counts()["universe_membership_events"]
    job = env.run(
        DiscoveryJobKind.QUALIFICATION, as_of=(env.clock.now() - timedelta(hours=1)).isoformat()
    )
    assert job.status is DiscoveryJobStatus.COMPLETED
    assert env.counts()["universe_membership_events"] == before
    refused = env.run(
        DiscoveryJobKind.MEMBERSHIP_UPDATE,
        layer="QUALIFIED_UNIVERSE",
        qualification_job_id=str(job.id),
    )
    assert refused.status is DiscoveryJobStatus.FAILED


def test_point_in_time_membership_has_no_survivorship_bias(env):
    pipeline(env)
    qualified_at = env.clock.now()
    env.clock.advance(timedelta(days=30))
    env.directory.set(
        *[
            r
            if r["symbol"] != "LIQ"
            else r | {"symbol": "LIQ_DELISTED", "status": "inactive", "tradable": False}
            for r in env.directory.records
        ]
    )
    env.discover()
    delisted_at = env.clock.now()
    liq = {"LIQ"}
    # Today LIQ is gone from every layer ...
    assert not liq & members(env, UniverseLayer.QUALIFIED_UNIVERSE)
    assert not liq & members(env, UniverseLayer.ACTIVE_WATCHLIST)
    assert not liq & members(env, UniverseLayer.DISCOVERY_POOL)
    # ... but a backtest of the period it was listed sees it.
    mid = qualified_at + timedelta(days=10)
    assert liq <= members(env, UniverseLayer.QUALIFIED_UNIVERSE, at=mid)
    assert liq <= members(env, UniverseLayer.ACTIVE_WATCHLIST, at=mid)
    assert not liq & members(
        env, UniverseLayer.QUALIFIED_UNIVERSE, at=delisted_at + timedelta(seconds=1)
    )
    # Before Atlas qualified anything, nothing was qualified.
    assert members(env, UniverseLayer.QUALIFIED_UNIVERSE, at=T0 - timedelta(days=1)) == set()


def test_membership_recorded_later_is_invisible_to_an_earlier_known_at(env):
    with session_scope(env.factory) as session:
        asset_id = env.asset_id("LIQ")
        membership.transition(
            session,
            asset_id=asset_id,
            layer=UniverseLayer.QUALIFIED_UNIVERSE,
            action=MembershipAction.ADDED,
            effective_at=T0,
            recorded_at=T0 + timedelta(days=5),
            reason_code="TEST",
            reason="recorded late (test)",
        )
    assert "LIQ" in members(env, UniverseLayer.QUALIFIED_UNIVERSE, at=T0 + timedelta(days=6))
    # What Atlas knew on day 1 about day 1 does not include it.
    day1 = T0 + timedelta(days=1)
    assert "LIQ" not in members(env, UniverseLayer.QUALIFIED_UNIVERSE, at=day1, known_at=day1)


def test_membership_cannot_be_back_dated(env):
    pipeline(env)
    with session_scope(env.factory) as session, pytest.raises(membership.MembershipError):
        membership.transition(
            session,
            asset_id=env.asset_id("LIQ"),
            layer=UniverseLayer.QUALIFIED_UNIVERSE,
            action=MembershipAction.REMOVED,
            effective_at=T0 - timedelta(days=1),
            recorded_at=env.clock.now(),
            reason_code="TEST",
            reason="back-dated (test)",
        )


def test_conflicting_transitions_from_the_same_state_cannot_both_commit(env):
    asset_id = env.asset_id("LIQ2")
    with session_scope(env.factory) as session:
        state = membership.current_states(session, UniverseLayer.QUALIFIED_UNIVERSE, [asset_id])
    assert state == {}
    first = env.factory()
    second = env.factory()
    try:
        for s in (first, second):
            membership.transition(
                s,
                asset_id=asset_id,
                layer=UniverseLayer.QUALIFIED_UNIVERSE,
                action=MembershipAction.ADDED,
                effective_at=T0,
                recorded_at=env.clock.now(),
                reason_code="TEST",
                reason="race (test)",
                previous=None,
                flush=False,
            )
        first.commit()
        with pytest.raises(IntegrityError):
            second.commit()
    finally:
        first.close()
        second.close()
    with session_scope(env.factory) as session:
        assert len(membership.history(session, asset_id, UniverseLayer.QUALIFIED_UNIVERSE)) == 1


def test_disqualification_removes_from_the_watchlist_with_reasons(env):
    pipeline(env)
    env.clock.advance(timedelta(days=2))
    # A corrected revision of every bar (MOCK): the stock now trades at 1 USD.
    seed_daily_bars(env, "LIQ2", close="1", volume=500, revision=1, ingested_at=env.clock.now())
    later = env.clock.now()
    with session_scope(env.factory) as session:
        rows = session.execute(
            select(QualificationEvaluationRecord.id).where(
                QualificationEvaluationRecord.asset_id == env.asset_id("LIQ2")
            )
        ).all()
    assert rows
    pipeline(env)
    with session_scope(env.factory) as session:
        events = membership.history(session, env.asset_id("LIQ2"))
        removed = [e for e in events if e.action is MembershipAction.REMOVED]
    layers = {e.layer for e in removed}
    assert UniverseLayer.QUALIFIED_UNIVERSE in layers
    assert UniverseLayer.ACTIVE_WATCHLIST in layers
    reason = next(e.reason for e in removed if e.layer is UniverseLayer.QUALIFIED_UNIVERSE)
    assert "min_price" in reason
    assert all(e.effective_at <= later for e in removed)


def test_rule_set_versions_are_immutable_and_audited(env):
    pipeline(env)
    from app.asset_discovery.rules import create_version
    from app.database.models import ActorType

    with session_scope(env.factory) as session:
        changed = DEFAULT_RULE_SETS["us-equity-iex"][1].model_copy(
            update={"min_price": Decimal("10")}
        )
        record = create_version(
            session,
            QualificationRuleSetRecord,
            code="us-equity-iex",
            config=changed,
            description="higher price floor (test)",
            created_by="test",
            actor_type=ActorType.USER,
            reason="test",
        )
        assert record.version == 2
        again = create_version(
            session,
            QualificationRuleSetRecord,
            code="us-equity-iex",
            config=changed,
            description="same",
            created_by="test",
            actor_type=ActorType.USER,
            reason="test",
        )
        assert again.id == record.id  # identical content is not a new version
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "qualification_rule_sets.version_created")
        ).all()
        assert [(a.new_state or {}).get("version") for a in audit] == [1, 2]
    job = env.run(DiscoveryJobKind.QUALIFICATION, prepare_history=False)
    with session_scope(env.factory) as session:
        rule_set = session.get(QualificationRuleSetRecord, uuid.UUID(job.summary["rule_set_id"]))
        assert rule_set is not None
        assert rule_set.version == 2
    assert evaluations(env, job.id)["LIQ2"].outcome is QualificationOutcome.QUALIFIED


def stub_ingestion_job(**fields: Any) -> DataIngestionJob:
    # A partial job: the qualification service reads only the ID and the status.
    return DataIngestionJob.model_construct(**fields)


class StubIngestion:
    """Records history requests instead of calling a provider (test double)."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def create_job(self, request, requested_by):
        self.requests.append(request)
        return stub_ingestion_job(job_id=uuid.uuid4())

    async def run_job(self, job_id):
        from app.data_engine.enums import IngestionJobStatus

        return stub_ingestion_job(job_id=job_id, status=IngestionJobStatus.COMPLETED)


def test_history_preparation_is_bounded_and_attributable_only(fresh_engine):
    env = make_env(fresh_engine, history_prep_max_assets=3, benchmark_symbol="BENCH")
    env.directory.set(
        *(asset_record(f"P{i}", attributes=("has_options",) if i % 2 else ()) for i in range(8)),
        asset_record("BENCH"),
        asset_record("SHARED", key="old-shared", status="inactive", tradable=False),
        asset_record("SHARED", key="new-shared"),
    )
    env.discover()
    stub = StubIngestion()
    service = QualificationService(
        env.factory,
        env.service._provider,
        MarketDataSettings(_env_file=None),
        env.service.settings,
        FixedClock(env.clock.now()),
        stub,  # type: ignore[arg-type]
    )
    import asyncio

    from app.asset_discovery.rules import RuleSetConfig

    config = RuleSetConfig.model_validate(DEFAULT_RULE_SETS["us-equity-iex"][1].model_dump())
    summary = asyncio.run(service.prepare_history(uuid.uuid4(), config))
    assert summary["requested"] == 3
    assert summary["skipped_mapping_starts_at_discovery"] == 1  # SHARED: mapping from discovery
    requested = [a for r in stub.requests for a in r.asset_ids]
    assert len(requested) == 3
    assert requested[0] == env.asset_id("BENCH")  # benchmark first
    assert env.asset_id("SHARED") not in requested
    assert all(str(r.timeframe) == "1Day" for r in stub.requests)

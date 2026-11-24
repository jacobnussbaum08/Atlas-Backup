"""Strategy evaluation worker and update detection against PostgreSQL (Phase 4A).

Bars are MOCK observations; strategies are TEST FIXTURE strategies; news articles are
constructed test data stored through the Phase 3C pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.asset_discovery import membership
from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.database.models import (
    StrategyEvaluationRecord,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
    StrategyRecord,
)
from app.database.sessions import session_scope
from app.strategies import triggers
from app.strategies.enums import EvaluationStatus, TriggerKind
from app.strategies.registry import discover
from app.strategies.runtime import StrategyContext
from app.tests.integration.strategy_support import (
    NOW,
    FailingStrategy,
    NewsFixtureStrategy,
    ProposingStrategy,
    add_reference,
    make_asset,
    make_lab,
    seed_bars,
)
from app.workers.strategy_evaluation import StrategyEvaluationWorker

pytestmark = pytest.mark.integration


def qualify(lab, asset_id, at=NOW - timedelta(days=30)):
    with session_scope(lab.factory) as session:
        membership.transition(
            session,
            asset_id=asset_id,
            layer=UniverseLayer.QUALIFIED_UNIVERSE,
            action=MembershipAction.ADDED,
            effective_at=at,
            recorded_at=at,
            reason_code="TEST",
            reason="TEST FIXTURE membership",
        )


def worker_for(lab, **settings) -> StrategyEvaluationWorker:
    values = lab.engine.settings.model_dump() | {"worker_enabled": True} | settings
    strategy_settings = type(lab.engine.settings)(_env_file=None, **values)
    lab.engine.settings = strategy_settings
    ctx = StrategyContext(strategy_settings, lab.registry, lab.engine, lab.factory, lab.clock)
    return StrategyEvaluationWorker(ctx, lab.clock)


def statuses(lab) -> list[tuple[str, EvaluationStatus]]:
    with session_scope(lab.factory) as session:
        rows = session.execute(
            select(StrategyEvaluationRecord.strategy_id, StrategyEvaluationRecord.status)
        ).all()
    return sorted((r[0], r[1]) for r in rows)


def test_bar_close_detection_needs_a_valid_stored_bar_for_the_newest_session(fresh_engine):
    lab = make_lab(fresh_engine)
    ready, stale, outside = (make_asset(lab.factory, s) for s in ("FXR", "FXS", "FXO"))
    seed_bars(lab.factory, ready, count=40)
    seed_bars(lab.factory, stale, count=40, skip={39})  # newest session not stored yet
    seed_bars(lab.factory, outside, count=40)
    for asset in (ready, stale):
        qualify(lab, asset)
    with session_scope(lab.factory) as session:
        scope = triggers.scope(session, NOW)
        updates = triggers.bar_close_updates(session, NOW, lab.engine.series, scope)
    assert set(scope) == {ready, stale}  # only the qualified universe
    assert [(u.asset_id, u.trigger, u.ref, u.timeframe) for u in updates] == [
        (ready, TriggerKind.BAR_CLOSE, "1Day:2026-11-03", "1Day")
    ]
    # 21:10 UTC: 3 Nov has closed but not settled, so the newest completed session is
    # still 2 Nov (both assets have that bar); 3 Nov is never treated as final early.
    with session_scope(lab.factory) as session:
        early = triggers.bar_close_updates(
            session, NOW.replace(hour=21, minute=10), lab.engine.series, scope
        )
    assert {u.ref for u in early} == {"1Day:2026-11-02"}
    assert {u.asset_id for u in early} == {ready, stale}


def test_week_end_also_triggers_the_weekly_timeframe(fresh_engine):
    friday = NOW + timedelta(days=3)  # Friday 6 November 2026, after the settle delay
    lab = make_lab(fresh_engine, now=friday)
    asset = make_asset(lab.factory, "FXW")
    seed_bars(lab.factory, asset, count=40, as_of=friday)
    with session_scope(lab.factory) as session:
        updates = triggers.bar_close_updates(session, friday, lab.engine.series, [asset])
    assert [u.ref for u in updates] == ["1Day:2026-11-06", "1Week:2026-W45"]


def test_worker_cycle_evaluates_once_and_isolates_failures(fresh_engine):
    lab = make_lab(fresh_engine, [ProposingStrategy(), FailingStrategy(), *discover()])
    asset = make_asset(lab.factory, "FXA")
    seed_bars(lab.factory, asset, count=40)
    qualify(lab, asset)
    worker = worker_for(lab)
    worker.sync()
    first = asyncio.run(worker.cycle())
    assert first["updates"] == 1
    # Phase 4B: the six families have logic and are planned on the daily bar close too.
    assert first["requests"] == 2 + len(discover())
    states = dict(statuses(lab))
    assert states["fixture_failing"] is EvaluationStatus.FAILED
    assert states["fixture_proposer"] is EvaluationStatus.PROPOSED
    families = {s.strategy_id for s in discover()}
    assert families <= set(states)
    # 40 MOCK bars are too few for their windows: every family finished without failing.
    assert all(states[f] is not EvaluationStatus.FAILED for f in families)
    lab.clock.advance(timedelta(minutes=1))
    second = asyncio.run(worker.cycle())
    # Same update again: finished ones are duplicates, the failing one is retried.
    assert second["outcomes"] == {"DUPLICATE": 1 + len(families), "FAILED": 1}
    with session_scope(lab.factory) as session:
        assert session.scalar(select(func.count()).select_from(StrategyProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(StrategyRecord)) == 8


def test_worker_records_expiry(fresh_engine):
    lab = make_lab(fresh_engine)
    asset = make_asset(lab.factory, "FXA")
    seed_bars(lab.factory, asset, count=40)
    qualify(lab, asset)
    worker = worker_for(lab)
    asyncio.run(worker.cycle())
    lab.clock.advance(timedelta(days=2))
    summary = asyncio.run(worker.cycle())
    assert summary["expired"] == 1
    with session_scope(lab.factory) as session:
        assert session.scalar(select(func.count()).select_from(StrategyProposalEventRecord)) == 1


def test_news_events_trigger_news_scheduled_strategies(fresh_engine):
    from app.news.enums import NewsJobKind
    from app.tests.fixtures.alpaca_news import news_item, news_page
    from app.tests.integration.news_support import make_news_env

    news = make_news_env(fresh_engine, now=NOW)
    lab = make_lab(fresh_engine, [NewsFixtureStrategy()])
    asset = news.assets["FXC"]
    with session_scope(lab.factory) as session:
        add_reference(session, asset, "FXC")  # point-in-time eligibility (Phase 4B)
    seed_bars(lab.factory, asset, count=40)
    qualify(lab, asset)
    worker = worker_for(lab)
    news.fake.add(news_page([news_item(7, NOW - timedelta(minutes=20), symbols=["FXC"])]))
    job = news.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=NOW - timedelta(hours=30),
        end=NOW,
        requested_by="test",
    )
    asyncio.run(news.service.run_job(job.id))
    summary = asyncio.run(worker.cycle())
    assert summary["updates"] == 2  # the completed session and the news event
    with session_scope(lab.factory) as session:
        rows = session.execute(
            select(StrategyEvaluationRecord.trigger, StrategyEvaluationRecord.status)
        ).all()
    assert sorted((t.value, s.value) for t, s in rows) == [
        ("BAR_CLOSE", "PROPOSED"),
        ("NEWS_EVENT", "PROPOSED"),
    ]
    # The news event is not detected twice.
    lab.clock.advance(timedelta(minutes=1))
    assert asyncio.run(worker.cycle())["outcomes"] == {"DUPLICATE": 1}


def test_disabled_worker_exits_without_work(fresh_engine):
    lab = make_lab(fresh_engine)
    worker = worker_for(lab, worker_enabled=False)
    assert asyncio.run(worker.run(asyncio.Event())) == 0
    assert statuses(lab) == []


def test_worker_loop_stops_on_signal(fresh_engine):
    lab = make_lab(fresh_engine)
    worker = worker_for(lab, worker_interval_seconds=5)

    async def run_briefly() -> int:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, stop.set)
        return await worker.run(stop)

    assert asyncio.run(run_briefly()) == 0

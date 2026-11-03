"""REAL Alpaca news smoke tests (opt-in, Phase 3C). Use the credentials in the local ``.env``.

Run from the repository root (never in ordinary test runs):

    ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... pytest -m live -s \
        backend/app/tests/live/test_news_live.py

* REST: authentication, a historical retrieval of a busy recent day with real pagination,
  persisted into a throw-away database, re-run to prove deduplication.
* Stream: the production worker against ``wss://stream.data.alpaca.markets/v1beta1/news``
  (separate from the stock stream): authentication and the ``*`` subscription must be
  confirmed; articles are persisted when Alpaca publishes some during the window (news is
  irregular, so their absence is reported, not failed), and the connection recovery job
  must complete.

Only counts, states and timestamps are printed; never credentials or article text.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.clocks import SystemClock
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.database.models import (
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsAssetMappingRecord,
    NewsIngestionJobRecord,
)
from app.database.sessions import session_scope
from app.news.config import NewsSettings
from app.news.enums import ArticleOrigin, NewsJobKind, NewsJobStatus
from app.news.providers.alpaca.adapter import AlpacaNewsProvider
from app.news.runtime import build_news_context
from app.workers.news_stream import EXIT_OK, NewsStreamWorker

pytestmark = pytest.mark.live

STREAM_SECONDS = 90


def _context(fresh_engine, **settings):
    clock = SystemClock()
    news = NewsSettings(_env_file=None, **settings)
    provider = AlpacaNewsProvider(get_alpaca_settings(), news, clock)
    return build_news_context(
        sessionmaker(bind=fresh_engine, expire_on_commit=False), clock, news, provider
    )


def test_real_rest_authentication_pagination_and_deduplication(fresh_engine):
    ctx = _context(fresh_engine)
    auth = asyncio.run(ctx.provider.check_authentication())
    print(f"\nnews REST authentication: {auth.status}")
    assert auth.status == "VERIFIED"

    end = ctx.clock.now() - timedelta(hours=1)
    start = end - timedelta(hours=6)  # all news for six hours: several 50-article pages
    job = ctx.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL, start=start, end=end, requested_by="live-test"
    )
    finished = asyncio.run(ctx.service.run_job(job.id))
    print(
        f"backfill {start:%Y-%m-%d %H:%M}..{end:%H:%M} UTC: status={finished.status.value} "
        f"pages={finished.pages_retrieved} received={finished.articles_received} "
        f"new={finished.articles_new} malformed={finished.malformed}"
    )
    assert finished.status is NewsJobStatus.COMPLETED
    assert finished.pages_retrieved >= 2, "expected real pagination over six hours of news"
    assert finished.articles_new > 0

    with session_scope(ctx.session_factory) as session:
        revisions = session.scalars(select(NewsArticleRevisionRecord)).all()
        assert all(r.origin is ArticleOrigin.HISTORICAL_REST for r in revisions)
        assert all(r.original_available_at is None for r in revisions)
        assert all(r.received_at > r.published_at for r in revisions)
        statuses = dict(
            session.execute(
                select(NewsAssetMappingRecord.status, func.count()).group_by(
                    NewsAssetMappingRecord.status
                )
            ).all()
        )
        print(f"mapping outcomes (no assets in this throw-away database): {statuses}")

    again = ctx.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL, start=start, end=end, requested_by="live-test"
    )
    rerun = asyncio.run(ctx.service.run_job(again.id))
    print(
        f"re-run: new={rerun.articles_new} revisions={rerun.revisions_new} dup={rerun.duplicates}"
    )
    assert rerun.status is NewsJobStatus.COMPLETED
    assert rerun.articles_new == 0
    # Articles updated by the provider between the two retrievals become revisions.
    assert rerun.duplicates + rerun.revisions_new == rerun.articles_received


def test_real_news_stream_worker(fresh_engine):
    ctx = _context(fresh_engine, stream_enabled=True)
    worker = NewsStreamWorker(context=ctx)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(STREAM_SECONDS)
        stop.set()
        return await task

    code = asyncio.run(scenario())
    print(
        f"\nnews stream: exit={code} auth={worker.auth_state.value} "
        f"subscription={worker.subscription_state.value} confirmed={sorted(worker.confirmed)} "
        f"counters={dict(worker.counters)} errors={[e['kind'] for e in worker.recent_errors]}"
    )
    assert code == EXIT_OK
    assert worker.last_authenticated_at is not None
    assert worker.last_subscribed_at is not None
    assert worker.confirmed == frozenset({"*"})
    with session_scope(ctx.session_factory) as session:
        live = session.scalars(
            select(NewsArticleRevisionRecord).where(
                NewsArticleRevisionRecord.origin == ArticleOrigin.REALTIME_STREAM
            )
        ).all()
        jobs = session.scalars(
            select(NewsIngestionJobRecord).where(
                NewsIngestionJobRecord.kind == NewsJobKind.STREAM_RECOVERY
            )
        ).all()
        articles = session.scalar(select(func.count()).select_from(NewsArticleRecord))
    print(
        f"articles received live: {len(live)}; recovery jobs: "
        f"{[(j.status.value, j.articles_new, j.duplicates) for j in jobs]}; stored: {articles}"
    )
    assert jobs
    assert jobs[0].status is NewsJobStatus.COMPLETED
    for revision in live:
        assert revision.original_available_at == revision.received_at
        assert revision.stream_session_id is not None
    if not live:
        print(f"no article was published during the {STREAM_SECONDS} s window (not a failure)")


def test_real_news_reconnection_restores_subscription_and_recovers(fresh_engine):
    """Drop the real socket mid-stream: the worker must reconnect, re-authenticate, restore
    the ``*`` subscription and run a second recovery retrieval for the interruption."""
    ctx = _context(fresh_engine, stream_enabled=True, stream_status_interval=1.0)
    worker = NewsStreamWorker(context=ctx, backoff_base=0.5)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        async with asyncio.timeout(30):
            while worker.last_subscribed_at is None:
                await asyncio.sleep(0.1)
        first = worker.last_subscribed_at
        worker.current_stream._ws.transport.abort()  # simulated network drop, no close frame
        async with asyncio.timeout(60):
            while worker.last_subscribed_at == first or worker.reconnect_count == 0:
                await asyncio.sleep(0.1)
        await asyncio.sleep(15)  # let the second recovery job finish
        stop.set()
        return await task

    code = asyncio.run(scenario())
    with session_scope(ctx.session_factory) as session:
        jobs = session.scalars(
            select(NewsIngestionJobRecord)
            .where(NewsIngestionJobRecord.kind == NewsJobKind.STREAM_RECOVERY)
            .order_by(NewsIngestionJobRecord.created_at)
        ).all()
        summary = [(j.status.value, j.articles_new, j.duplicates) for j in jobs]
    print(
        f"\nreconnect: exit={code} reconnects={worker.reconnect_count} "
        f"subscription={worker.subscription_state.value} confirmed={sorted(worker.confirmed)} "
        f"recovery jobs={summary} counters={dict(worker.counters)}"
    )
    assert code == EXIT_OK
    assert worker.reconnect_count == 1
    assert worker.confirmed == frozenset({"*"})
    assert len(jobs) == 2
    assert all(j.status is NewsJobStatus.COMPLETED for j in jobs)

"""The news stream worker against a FAKE WebSocket server, a FAKE REST server and real
PostgreSQL (Phase 3C). Every article is a constructed TEST FIXTURE; nothing here contacts
Alpaca (see tests/live)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.database.models import (
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsIngestionJobRecord,
    NewsStreamSessionRecord,
    NewsStreamStatusRecord,
)
from app.database.sessions import session_scope
from app.news.enums import ArticleOrigin, NewsJobKind, NewsJobStatus
from app.news.ingestion import NewsIngestionService
from app.news.runtime import NewsContext
from app.tests.fixtures.alpaca_news import fake_news_provider, news_item, news_page, news_settings
from app.tests.fixtures.alpaca_stream import FakeAlpacaStreamServer, Script, eventually, frame
from app.tests.integration.news_support import NOW, T0, make_news_env
from app.workers.news_stream import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,
    EXIT_STREAM_FAILED,
    NewsStreamWorker,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    return make_news_env(fresh_engine)


def worker_for(env, server, **settings):
    values = {"stream_enabled": True, "stream_status_interval": 1.0} | settings
    provider = fake_news_provider(
        env.fake, now=NOW, clock=env.clock, stream_url=server.url if server else None, **values
    )
    config = news_settings(**values)
    service = NewsIngestionService(env.factory, provider, config, env.clock)
    context = NewsContext(config, provider, service, env.factory, env.clock)
    return NewsStreamWorker(context=context, backoff_base=0.01, jitter=lambda: 1.0)


def count(env, model, *where):
    with session_scope(env.factory) as session:
        return len(session.scalars(select(model).where(*where)).all())


def jobs(env):
    with session_scope(env.factory) as session:
        return list(
            session.scalars(
                select(NewsIngestionJobRecord).order_by(NewsIngestionJobRecord.created_at)
            ).all()
        )


def status(env):
    with session_scope(env.factory) as session:
        return session.get(NewsStreamStatusRecord, "alpaca")


def live(article_id, **kwargs):
    return frame(news_item(article_id, T0, stream=True, **kwargs))


def test_streams_persists_and_recovers_the_connection_window(env):
    # Recovery over REST returns the streamed article again (overlap) and one it missed.
    env.fake.add(news_page([news_item(1, T0, symbols=["FXC"]), news_item(2, T0)]))

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[[live(1, symbols=["FXC"])]])) as server:
            worker = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(
                lambda: (
                    count(env, NewsArticleRecord) == 2
                    and any(j.status is NewsJobStatus.COMPLETED for j in jobs(env))
                ),
                timeout=20,
            )
            stop.set()
            return await task, worker, server.connections

    code, worker, connections = asyncio.run(scenario())
    assert code == EXIT_OK
    assert connections[0].subscribe_messages == [{"action": "subscribe", "news": ["*"]}]
    with session_scope(env.factory) as session:
        streamed = session.scalars(
            select(NewsArticleRevisionRecord)
            .join(NewsArticleRecord)
            .where(NewsArticleRecord.provider_article_id == "1")
        ).all()
        missed = session.scalars(
            select(NewsArticleRecord).where(NewsArticleRecord.provider_article_id == "2")
        ).one()
        assert len(streamed) == 1  # the overlap was recognised, not stored twice
        assert streamed[0].origin is ArticleOrigin.REALTIME_STREAM
        assert streamed[0].original_available_at == NOW
        assert streamed[0].stream_session_id is not None
        assert missed.first_origin is ArticleOrigin.HISTORICAL_REST
    [recovery] = jobs(env)
    assert recovery.kind is NewsJobKind.STREAM_RECOVERY
    assert recovery.range_end - recovery.range_start == timedelta(seconds=300)
    assert (recovery.articles_new, recovery.duplicates) == (1, 1)
    assert recovery.stream_session_id is not None
    row = status(env)
    assert row.connection_state == "STOPPED"
    assert row.counters["articles_new"] == 1
    assert row.counters["recovery_jobs"] == 1
    assert row.counters["recovery_articles_new"] == 1
    assert worker.counters["frames_received"] >= 1


def test_reconnects_restores_the_subscription_and_recovers_the_gap(env):
    env.fake.add(news_page([])).add(news_page([news_item(9, T0)]))

    async def scenario():
        script = Script(frames=[[live(1)], [live(3)]], drop_after_frames=[True, False])
        async with FakeAlpacaStreamServer(script) as server:
            worker = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(
                lambda: count(env, NewsArticleRecord) == 3 and len(jobs(env)) == 2, timeout=20
            )
            await eventually(lambda: all(j.completed_at for j in jobs(env)), timeout=20)
            stop.set()
            return await task, worker, server.connections

    code, worker, connections = asyncio.run(scenario())
    assert code == EXIT_OK
    assert len(connections) == 2
    assert [c.subscribe_messages for c in connections] == [
        [{"action": "subscribe", "news": ["*"]}],
        [{"action": "subscribe", "news": ["*"]}],  # restored on the new connection
    ]
    assert worker.reconnect_count == 1
    assert count(env, NewsStreamSessionRecord) == 2
    with session_scope(env.factory) as session:
        sessions = session.scalars(
            select(NewsStreamSessionRecord).order_by(NewsStreamSessionRecord.started_at)
        ).all()
        assert all(s.ended_at is not None for s in sessions)
        assert "closed unexpectedly" in (sessions[0].end_reason or "")
    assert [j.kind for j in jobs(env)] == [NewsJobKind.STREAM_RECOVERY] * 2
    assert status(env).reconnect_count == 1


def test_rejected_credentials_fail_permanently_without_retrying(env):
    async def scenario():
        async with FakeAlpacaStreamServer(Script(auth_errors=[(402, "auth failed")])) as server:
            worker = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(lambda: worker.state.value == "FAILED", timeout=10)
            await asyncio.sleep(0.2)
            stop.set()
            return await task, server.connections

    code, connections = asyncio.run(scenario())
    assert code == EXIT_STREAM_FAILED
    assert len(connections) == 1
    row = status(env)
    assert row.connection_state == "FAILED"
    assert row.auth_state == "REJECTED"
    assert "auth failed" in (row.last_error or "")
    assert jobs(env) == []


def test_recovery_outage_is_recorded_and_streaming_continues(env):
    for _ in range(6):
        env.fake.add_error(503, "down")

    async def scenario():
        async with FakeAlpacaStreamServer(Script(frames=[[live(1)]])) as server:
            worker = worker_for(env, server, historical_max_retries=1)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(
                lambda: (
                    count(env, NewsArticleRecord) == 1
                    and any(j.status is NewsJobStatus.FAILED for j in jobs(env))
                ),
                timeout=20,
            )
            stop.set()
            return await task, worker

    code, worker = asyncio.run(scenario())
    assert code == EXIT_OK
    assert worker.counters["recovery_jobs_failed"] == 1
    assert any(e["kind"] == "recovery" for e in worker.recent_errors)


def test_disabled_worker_records_its_state_and_exits(env):
    worker = worker_for(env, None, stream_enabled=False)
    assert asyncio.run(worker.run(asyncio.Event())) == EXIT_OK
    row = status(env)
    assert row.enabled is False
    assert row.connection_state == "DISABLED"


def test_only_one_worker_per_provider(env):
    async def scenario():
        async with FakeAlpacaStreamServer(Script()) as server:
            env.fake.add(news_page([]))
            first = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(first.run(stop))
            await eventually(lambda: first.state.value == "SUBSCRIBED", timeout=10)
            second = await worker_for(env, server).run(asyncio.Event())
            stop.set()
            return second, await task

    second, first = asyncio.run(scenario())
    assert second == EXIT_ALREADY_RUNNING
    assert first == EXIT_OK


def test_restart_recovers_from_where_the_previous_worker_stopped(env):
    """The recovery window after a restart starts at the previous session's end (minus the
    overlap), not at its start: a long session must not widen the retrieval."""
    env.fake.add(news_page([])).add(news_page([]))

    async def run_once(advance_before_stop):
        async with FakeAlpacaStreamServer(Script()) as server:
            worker = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(lambda: worker.state.value == "SUBSCRIBED", timeout=10)
            await eventually(lambda: all(j.completed_at for j in jobs(env)), timeout=10)
            env.clock.advance(advance_before_stop)
            stop.set()
            return await task

    assert asyncio.run(run_once(timedelta(minutes=10))) == EXIT_OK  # session NOW .. NOW+10m
    env.clock.set(NOW + timedelta(hours=1))
    assert asyncio.run(run_once(timedelta(0))) == EXIT_OK
    second = jobs(env)[1]
    assert second.range_start == NOW + timedelta(minutes=10) - timedelta(seconds=300)
    assert second.range_end == NOW + timedelta(hours=1)


def test_restart_after_a_crash_recovers_from_the_last_heartbeat(env):
    from app.tests.integration.news_support import stream_session

    stream_session(env.factory, NOW - timedelta(hours=5))  # never closed: the worker died
    with session_scope(env.factory) as session:
        session.add(
            NewsStreamStatusRecord(
                provider="alpaca",
                enabled=True,
                connection_state="SUBSCRIBED",
                auth_state="AUTHENTICATED",
                subscription_state="ACTIVE",
                heartbeat_at=NOW - timedelta(hours=2),
                last_message_at=NOW - timedelta(hours=3),
                last_persisted_at=NOW - timedelta(hours=3) + timedelta(seconds=1),
            )
        )
    env.fake.add(news_page([]))

    async def scenario():
        async with FakeAlpacaStreamServer(Script()) as server:
            worker = worker_for(env, server)
            stop = asyncio.Event()
            task = asyncio.create_task(worker.run(stop))
            await eventually(lambda: any(j.completed_at for j in jobs(env)), timeout=10)
            stop.set()
            return await task

    assert asyncio.run(scenario()) == EXIT_OK
    [recovery] = jobs(env)
    assert recovery.range_start == NOW - timedelta(hours=2) - timedelta(seconds=300)
    with session_scope(env.factory) as session:
        crashed = session.scalars(
            select(NewsStreamSessionRecord).where(
                NewsStreamSessionRecord.worker_id == "test-worker"
            )
        ).one()
        assert crashed.end_reason is not None
        assert crashed.end_reason.startswith("interrupted")

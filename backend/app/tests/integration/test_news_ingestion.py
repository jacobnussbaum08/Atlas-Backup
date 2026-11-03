"""Historical news jobs and point-in-time reads against real PostgreSQL (Phase 3C).

The REST provider is the FAKE server (``FakeAlpaca``) returning TEST FIXTURE articles."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.exceptions import InvalidRequestError
from app.database.models import NewsArticleRecord, NewsIngestionJobRecord
from app.database.sessions import session_scope
from app.news import queries
from app.news.enums import ArticleOrigin, NewsJobKind, NewsJobStatus
from app.news.ingestion import INTERRUPTED_REASON
from app.news.providers.alpaca.normalize import normalize_article
from app.tests.fixtures.alpaca_news import news_item, news_page
from app.tests.integration.news_support import NOW, T0, later, make_news_env

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    return make_news_env(fresh_engine)


def run(env, **overrides):
    values = {
        "kind": NewsJobKind.HISTORICAL_BACKFILL,
        "start": T0 - timedelta(hours=1),
        "end": NOW,
        "symbols": ["FXC", "TWO"],
        "requested_by": "test",
    } | overrides
    job = env.service.create_job(**values)
    return asyncio.run(env.service.run_job(job.id))


def test_backfill_job_paginates_persists_and_is_idempotent(env):
    env.fake.add(
        news_page([news_item(1, T0, symbols=["FXC"]), news_item(2, T0, symbols=["TWO"])], "p2")
    ).add(news_page([news_item(3, T0 + timedelta(minutes=5), symbols=["FXC", "TWO"])]))
    job = run(env)
    assert job.status is NewsJobStatus.COMPLETED
    assert (job.pages_retrieved, job.articles_received, job.articles_new) == (2, 3, 3)
    assert (job.revisions_new, job.duplicates, job.malformed) == (3, 0, 0)
    assert job.completed_at is not None
    assert env.fake.params(0)["symbols"] == "FXC,TWO"
    assert env.fake.params(1)["page_token"] == "p2"

    # The same range again: every article is recognised; nothing new is stored.
    env.fake.add(
        news_page([news_item(1, T0, symbols=["FXC"]), news_item(2, T0, symbols=["TWO"])], "p2")
    ).add(news_page([news_item(3, T0 + timedelta(minutes=5), symbols=["FXC", "TWO"])]))
    again = run(env)
    assert (again.articles_new, again.revisions_new, again.duplicates) == (0, 0, 3)
    with session_scope(env.factory) as session:
        assert session.scalar(select(NewsArticleRecord.id).limit(1)) is not None
        assert len(session.scalars(select(NewsArticleRecord)).all()) == 3


def test_malformed_items_are_counted_and_sampled(env):
    env.fake.add(news_page([news_item(1, T0), {**news_item(2, T0), "headline": ""}]))
    job = run(env)
    assert job.status is NewsJobStatus.COMPLETED
    assert (job.articles_new, job.malformed) == (1, 1)
    assert job.params["malformed_samples"] == [{"id": "2", "reason": "article has no headline"}]


def test_provider_outage_fails_the_job_and_keeps_completed_pages(env):
    env.fake.add(news_page([news_item(1, T0)], "p2"))
    for _ in range(6):
        env.fake.add_error(503, "service unavailable")
    job = run(env)
    assert job.status is NewsJobStatus.FAILED
    assert "503" in (job.error or "")
    assert (job.pages_retrieved, job.articles_new) == (1, 1)  # the first page stays stored


def test_rejected_credentials_fail_the_job_without_retrying(env):
    env.fake.add_error(401, "unauthorized")
    job = run(env)
    assert job.status is NewsJobStatus.FAILED
    assert "credentials" in (job.error or "")
    assert len(env.fake.requests) == 1


def test_job_validation(env):
    with pytest.raises(InvalidRequestError, match="end must be after start"):
        run(env, start=NOW, end=NOW - timedelta(hours=1))
    with pytest.raises(InvalidRequestError, match="MAX_RANGE"):
        run(env, start=NOW - timedelta(days=400))
    with pytest.raises(InvalidRequestError, match="future"):
        run(env, end=NOW + timedelta(hours=1))
    with pytest.raises(InvalidRequestError, match="not a valid"):
        run(env, symbols=["not a symbol"])


def test_stale_running_jobs_are_reclaimed_and_cannot_finish(env):
    stale = env.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=T0,
        end=NOW,
        requested_by="test",
    )
    with session_scope(env.factory) as session:
        session.execute(
            update(NewsIngestionJobRecord)
            .where(NewsIngestionJobRecord.id == stale.id)
            .values(status=NewsJobStatus.RUNNING, heartbeat_at=NOW - timedelta(minutes=10))
        )
    env.service.create_job(  # creating any job sweeps stale ones
        kind=NewsJobKind.HISTORICAL_BACKFILL, start=T0, end=NOW, requested_by="test"
    )
    reclaimed = env.service.get_job(stale.id)
    assert reclaimed.status is NewsJobStatus.FAILED
    assert reclaimed.error == INTERRUPTED_REASON


def test_point_in_time_view_never_leaks_later_retrievals(env):
    """An article published at T0 but retrieved at NOW did not exist for Atlas before NOW;
    a correction stored later is invisible before it was stored."""
    env.fake.add(news_page([news_item(1, T0, headline="Fixture Corp Earnings", symbols=["FXC"])]))
    run(env)
    env.clock.set(later(60))
    revised = news_item(
        1,
        T0,
        headline="Fixture Corp Earnings (Corrected)",
        symbols=["FXC"],
        updated_at=T0 + timedelta(hours=2),
    )
    with session_scope(env.factory) as session:
        env.service.store(session).ingest(
            [
                normalize_article(
                    revised,
                    origin=ArticleOrigin.HISTORICAL_REST,
                    received_at=later(60),
                    store_content=False,
                )
            ],
            job_id=env.service.list_jobs(limit=1)[0].id,
        )

    def view(known_at):
        with session_scope(env.factory) as session:
            articles, _ = queries.feed(
                session,
                queries.FeedFilter(known_at=known_at),
                at=known_at or env.clock.now(),
                half_life_hours=24,
            )
            return articles

    assert view(T0 + timedelta(hours=1)) == []  # published, but Atlas did not have it yet
    [first] = view(later(1))
    assert first.headline == "Fixture Corp Earnings"
    assert first.revision == 1
    assert first.original_available_at is None
    assert first.first_origin is ArticleOrigin.HISTORICAL_REST
    [current] = view(None)
    assert current.headline == "Fixture Corp Earnings (Corrected)"
    assert current.revision == 2
    assert current.event is not None
    assert current.event.available_at >= later(60)

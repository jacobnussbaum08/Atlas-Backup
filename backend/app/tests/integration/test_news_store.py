"""News persistence against real PostgreSQL (Phase 3C): deduplication, revisions, asset
mapping, classification, timestamps and immutability. Articles are TEST FIXTURES."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database.models import (
    EventAssetRelationshipRecord,
    EventClassificationRecord,
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsAssetMappingRecord,
    NewsEventRecord,
    NewsSourceRecord,
)
from app.database.sessions import session_scope
from app.news.enums import (
    ArticleOrigin,
    EventCategory,
    EventScope,
    MappingStatus,
    NewsJobKind,
    RevisionReason,
)
from app.news.providers.alpaca.normalize import normalize_article
from app.tests.fixtures.alpaca_news import news_item
from app.tests.integration.news_support import NOW, T0, later, make_news_env, stream_session

pytestmark = pytest.mark.integration


@pytest.fixture
def env(fresh_engine):
    return make_news_env(fresh_engine)


def observe(item, *, origin=ArticleOrigin.HISTORICAL_REST, received_at=NOW):
    return normalize_article(item, origin=origin, received_at=received_at, store_content=False)


def job(env):
    return env.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=T0 - timedelta(days=1),
        end=NOW,
        requested_by="test",
    ).id


def ingest(env, articles, **ids):
    with session_scope(env.factory) as session:
        return env.service.store(session).ingest(articles, **ids)


def rows(env, model, *where):
    with session_scope(env.factory) as session:
        return list(session.scalars(select(model).where(*where)).all())


def article(env, provider_id: str):
    [found] = rows(env, NewsArticleRecord, NewsArticleRecord.provider_article_id == provider_id)
    return found


def test_first_observation_maps_classifies_and_records_availability(env):
    job_id = job(env)
    item = news_item(
        1001,
        T0,
        headline="Fixture Corp Beats Earnings Estimates",
        summary="The company raised its dividend.",
        symbols=["FXC", "OLD", "NEW", "RUZ", "ZZZZ", "BTCUSD", "BAD!"],
        url="https://example.com/fixture",
    )
    result = ingest(env, [observe(item)], job_id=job_id)
    assert (result.new_articles, result.new_revisions, result.duplicates) == (1, 1, 0)

    a = article(env, "1001")
    assert a.published_at == T0  # the provider's time, never Atlas's
    assert a.first_received_at == NOW
    assert a.first_available_at >= a.first_received_at
    assert a.first_origin is ArticleOrigin.HISTORICAL_REST
    assert a.first_live_received_at is None
    assert a.current_revision == 1
    assert a.primary_category is EventCategory.EARNINGS
    assert a.scope is EventScope.ASSET
    assert set(a.mapped_asset_ids) == {env.assets["FXC"], env.assets["OLD"]}

    [rev] = rows(env, NewsArticleRevisionRecord, NewsArticleRevisionRecord.article_id == a.id)
    assert rev.reason is RevisionReason.FIRST_OBSERVATION
    assert rev.origin is ArticleOrigin.HISTORICAL_REST
    assert rev.original_available_at is None  # a backfill never claims live availability
    assert rev.received_at == NOW
    assert rev.available_at >= rev.received_at
    assert rev.ingestion_job_id == job_id
    assert rev.content is None
    assert rev.has_body is False

    mappings = {
        m.provider_symbol: m
        for m in rows(env, NewsAssetMappingRecord, NewsAssetMappingRecord.article_id == a.id)
    }
    assert {s: m.status for s, m in mappings.items()} == {
        "FXC": MappingStatus.MAPPED,
        "OLD": MappingStatus.MAPPED,  # inactive now, but mapped at publication
        "NEW": MappingStatus.NOT_EFFECTIVE,  # only observed later: not forced
        "RUZ": MappingStatus.AMBIGUOUS,  # changed hands 2.5 h before publication
        "ZZZZ": MappingStatus.UNKNOWN_SYMBOL,
        "BTCUSD": MappingStatus.UNKNOWN_SYMBOL,
        "BAD!": MappingStatus.INVALID_SYMBOL,
    }
    assert mappings["OLD"].asset_status == "INACTIVE"
    assert "inactive" in mappings["OLD"].reason
    assert mappings["RUZ"].asset_id is None
    assert {c["asset_symbol"] for c in mappings["RUZ"].candidates} == {"RUZ", "RUZB"}
    assert "crypto" in mappings["BTCUSD"].reason
    assert all(m.resolved_for == T0 for m in mappings.values())

    [event] = rows(env, NewsEventRecord, NewsEventRecord.article_id == a.id)
    assert event.classifier_version == "news-taxonomy-v1/keyword-rules-v2"
    assert event.event_time == T0
    assert event.available_at == rev.available_at
    categories = rows(
        env, EventClassificationRecord, EventClassificationRecord.event_id == event.id
    )
    assert {c.category for c in categories} >= {
        EventCategory.EARNINGS,
        EventCategory.CORPORATE_ACTIONS,
    }
    assert all(c.verification.value == "UNVERIFIED" for c in categories)
    relations = rows(
        env, EventAssetRelationshipRecord, EventAssetRelationshipRecord.event_id == event.id
    )
    by_asset = {r.asset_id: r for r in relations}
    assert set(by_asset) == {env.assets["FXC"], env.assets["OLD"]}
    fxc = by_asset[env.assets["FXC"]]
    assert fxc.relevance_factors["tagged_symbols"] == 7
    assert fxc.relevance_factors["headline_mention"] == "name"
    assert str(fxc.static_relevance) == "0.7429"  # 0.40 + 0.30/7 + 0.30

    [source] = rows(env, NewsSourceRecord)
    assert (source.code, source.reliability.value) == ("fixturewire", "NOT_ASSESSED")


def test_duplicates_are_recognised_across_rest_and_stream(env):
    job_id = job(env)
    session_id = stream_session(env.factory, later(5))
    item = news_item(2001, T0, symbols=["FXC"])
    ingest(env, [observe(item)], job_id=job_id)
    again = ingest(env, [observe(item, received_at=later(1))], job_id=job_id)
    live = ingest(
        env,
        [observe(item, origin=ArticleOrigin.REALTIME_STREAM, received_at=later(5))],
        stream_session_id=session_id,
    )
    assert (again.duplicates, live.duplicates) == (1, 1)
    a = article(env, "2001")
    assert a.observation_count == 3
    assert a.current_revision == 1
    assert a.first_origin is ArticleOrigin.HISTORICAL_REST  # how Atlas first got it
    assert a.first_live_received_at == later(5)  # later live evidence is recorded
    assert len(rows(env, NewsArticleRevisionRecord)) == 1
    assert len(rows(env, NewsEventRecord)) == 1


def test_live_observation_records_original_availability_and_backfill_overlap_is_idempotent(env):
    session_id = stream_session(env.factory, T0)
    item = news_item(3001, T0, symbols=["FXC"])
    received = T0 + timedelta(seconds=2)
    ingest(
        env,
        [observe(item, origin=ArticleOrigin.REALTIME_STREAM, received_at=received)],
        stream_session_id=session_id,
    )
    overlap = ingest(env, [observe(item)], job_id=job(env))
    assert overlap.duplicates == 1
    [rev] = rows(env, NewsArticleRevisionRecord)
    assert rev.origin is ArticleOrigin.REALTIME_STREAM
    assert rev.original_available_at == received
    assert rev.stream_session_id == session_id
    assert rev.ingestion_job_id is None
    a = article(env, "3001")
    assert a.first_origin is ArticleOrigin.REALTIME_STREAM
    assert a.first_live_received_at == received


def test_updates_and_corrections_add_revisions_without_rewriting_history(env):
    job_id = job(env)
    v1 = news_item(4001, T0, headline="Fixture Corp To Acquire Example", symbols=["FXC"])
    ingest(env, [observe(v1)], job_id=job_id)
    v2 = news_item(
        4001,
        T0,
        headline="Fixture Corp Completes Acquisition Of Example",
        symbols=["FXC", "TWO"],
        updated_at=T0 + timedelta(hours=1),
    )
    ingest(env, [observe(v2, received_at=later(1))], job_id=job_id)
    same_time_edit = {**v2, "summary": "Corrected summary."}
    ingest(env, [observe(same_time_edit, received_at=later(2))], job_id=job_id)
    stale = {**v1, "summary": "An older variant arriving late."}
    ingest(env, [observe(stale, received_at=later(3))], job_id=job_id)

    revisions = sorted(rows(env, NewsArticleRevisionRecord), key=lambda r: r.revision)
    assert [(r.revision, r.reason) for r in revisions] == [
        (1, RevisionReason.FIRST_OBSERVATION),
        (2, RevisionReason.PROVIDER_UPDATE),
        (3, RevisionReason.CONTENT_CHANGED),
        (4, RevisionReason.LATE_EARLIER_VERSION),
    ]
    assert revisions[0].headline == "Fixture Corp To Acquire Example"  # untouched
    assert [r.received_at for r in revisions] == [NOW, later(1), later(2), later(3)]
    a = article(env, "4001")
    assert a.current_revision == 3  # the late, older version never becomes current
    assert a.summary == "Corrected summary."
    assert set(a.mapped_asset_ids) == {env.assets["FXC"], env.assets["TWO"]}
    # Every revision has its own mapping and event.
    assert len(rows(env, NewsEventRecord)) == 4
    assert {
        m.provider_symbol
        for m in rows(
            env, NewsAssetMappingRecord, NewsAssetMappingRecord.revision_id == revisions[1].id
        )
    } == {"FXC", "TWO"}


def test_a_missing_body_never_contradicts_a_stored_body(env):
    session_id = stream_session(env.factory, T0)
    with_body = news_item(5001, T0, content="<p>Body text</p>")
    ingest(
        env,
        [observe(with_body, origin=ArticleOrigin.REALTIME_STREAM, received_at=T0)],
        stream_session_id=session_id,
    )
    without = {**with_body, "content": ""}
    assert ingest(env, [observe(without)], job_id=job(env)).duplicates == 1
    edited = {**with_body, "content": "<p>Edited body</p>"}
    changed = ingest(
        env,
        [observe(edited, origin=ArticleOrigin.REALTIME_STREAM, received_at=later(1))],
        stream_session_id=session_id,
    )
    assert changed.new_revisions == 1
    revisions = sorted(rows(env, NewsArticleRevisionRecord), key=lambda r: r.revision)
    assert revisions[1].reason is RevisionReason.CONTENT_CHANGED
    assert revisions[0].body_sha256 != revisions[1].body_sha256
    assert all(r.content is None for r in revisions)  # bodies are not retained by default


def test_unmapped_articles_are_kept_with_their_scope(env):
    job_id = job(env)
    ingest(
        env,
        [
            observe(news_item(6001, T0, headline="US CPI Rises 0.3% In February", symbols=[])),
            observe(
                news_item(6002, T0, headline="Unknown Co Announces Partnership", symbols=["ZZZZ"])
            ),
        ],
        job_id=job_id,
    )
    macro, company = article(env, "6001"), article(env, "6002")
    assert (macro.scope, macro.primary_category) == (EventScope.MARKET, EventCategory.INFLATION)
    assert macro.mapped_asset_ids == []
    assert company.scope is EventScope.UNLINKED
    assert company.mapped_asset_ids == []


def test_revisions_events_and_classifications_are_append_only(env):
    item = news_item(7001, T0, headline="Fixture Corp Earnings", symbols=["FXC"])
    ingest(env, [observe(item)], job_id=job(env))
    for table in (
        "news_article_revisions",
        "news_asset_mappings",
        "news_events",
        "event_classifications",
        "event_asset_relationships",
    ):
        with (
            pytest.raises(DBAPIError, match="is append-only"),
            env.engine.begin() as conn,
        ):
            conn.execute(text(f"DELETE FROM {table}"))  # fixed table names
    with pytest.raises(DBAPIError), env.engine.begin() as conn:
        conn.execute(text("UPDATE news_article_revisions SET headline = 'rewritten'"))


def test_database_refuses_impossible_availability_and_verified_estimates(env):
    ingest(env, [observe(news_item(8001, T0, headline="Fixture Corp Earnings"))], job_id=job(env))
    [rev] = rows(env, NewsArticleRevisionRecord)
    with (
        pytest.raises(IntegrityError, match="original_availability_only_live"),
        session_scope(env.factory) as session,
    ):
        session.add(
            NewsArticleRevisionRecord(
                article_id=rev.article_id,
                revision=2,
                reason=RevisionReason.CONTENT_CHANGED,
                origin=ArticleOrigin.HISTORICAL_REST,
                headline="x",
                published_at=T0,
                has_body=False,
                content_sha256="0" * 64,
                received_at=NOW,
                processed_at=NOW,
                available_at=NOW,
                original_available_at=NOW,  # a REST retrieval claiming live availability
                ingestion_job_id=rev.ingestion_job_id,
            )
        )
    [event] = rows(env, NewsEventRecord)
    with (
        pytest.raises(IntegrityError, match="atlas_labels_unverified"),
        session_scope(env.factory) as session,
    ):
        session.add(
            EventClassificationRecord(
                event_id=event.id,
                category=EventCategory.LITIGATION,
                source="ATLAS",
                method="KEYWORD_RULES",
                confidence="MEDIUM",
                verification="PROVIDER_ASSERTED",
                is_primary=False,
                score=2,
            )
        )


def test_concurrent_writers_never_duplicate_an_article(env):
    job_id = job(env)
    item = news_item(9001, T0, symbols=["FXC"])
    results = []
    barrier = threading.Barrier(4)

    def write():
        barrier.wait()
        results.append(ingest(env, [observe(item)], job_id=job_id))

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.new_articles for r in results) == 1
    assert sum(r.duplicates for r in results) == 3
    assert len(rows(env, NewsArticleRecord)) == 1
    assert len(rows(env, NewsArticleRevisionRecord)) == 1


def test_reclassification_adds_a_new_version_without_leaking_into_the_past(env, monkeypatch):
    from app.news import ingestion as ingestion_module
    from app.news import queries
    from app.news import store as store_module
    from app.news.classification import classify as real_classify

    item = news_item(10001, T0, headline="Fixture Corp Earnings Beat", symbols=["FXC"])
    ingest(env, [observe(item)], job_id=job(env))
    assert env.service.reclassify_pending() == 0  # already classified under this version

    # A later rule version (simulated): everything is re-labelled LITIGATION.
    def next_version(**kwargs):
        result = real_classify(**kwargs)
        from dataclasses import replace

        lit = [
            replace(a, category=EventCategory.LITIGATION)
            for a in result.assignments
            if a.is_primary
        ]
        return replace(
            result,
            primary=EventCategory.LITIGATION,
            assignments=tuple(lit),
            classifier_version="news-taxonomy-v1/keyword-rules-vNEXT",
        )

    monkeypatch.setattr(store_module, "classify", next_version)
    monkeypatch.setattr(store_module, "CLASSIFIER_VERSION", "news-taxonomy-v1/keyword-rules-vNEXT")
    monkeypatch.setattr(
        ingestion_module, "CLASSIFIER_VERSION", "news-taxonomy-v1/keyword-rules-vNEXT"
    )
    env.clock.set(later(30))
    assert env.service.reclassify_pending() == 1
    assert env.service.reclassify_pending() == 0  # idempotent

    events = sorted(rows(env, NewsEventRecord), key=lambda e: e.id)
    assert [e.classifier_version for e in events] == [
        "news-taxonomy-v1/keyword-rules-v2",
        "news-taxonomy-v1/keyword-rules-vNEXT",
    ]
    assert events[1].available_at == later(30)  # computed now, not back-dated
    assert article(env, "10001").primary_category is EventCategory.LITIGATION
    relations = rows(
        env, EventAssetRelationshipRecord, EventAssetRelationshipRecord.event_id == events[1].id
    )
    assert [r.asset_id for r in relations] == [env.assets["FXC"]]  # stored mapping reused

    def primary(known_at):
        with session_scope(env.factory) as session:
            [a], _ = queries.feed(
                session, queries.FeedFilter(known_at=known_at), at=known_at, half_life_hours=24
            )
            assert a.event is not None
            return a.event.primary_category

    assert primary(later(10)) is EventCategory.EARNINGS  # what was known then
    assert primary(later(31)) is EventCategory.LITIGATION

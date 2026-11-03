"""News API against real PostgreSQL (Phase 3C). Articles are TEST FIXTURES."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.database.models import NewsStreamStatusRecord
from app.database.sessions import session_scope
from app.main import create_app
from app.news.enums import ArticleOrigin, NewsJobKind
from app.news.providers.alpaca.normalize import normalize_article
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET
from app.tests.fixtures.alpaca_news import news_item, news_page
from app.tests.integration.news_support import NOW, T0, later, make_news_env, stream_session

pytestmark = pytest.mark.integration
BASE = "/api/v1/news"


@pytest.fixture
def env(fresh_engine):
    return make_news_env(fresh_engine)


@pytest.fixture
def client(env, make_settings):
    def build(**settings) -> TestClient:
        app = create_app(
            make_settings(**settings), clock=env.clock, health_checks={}, news=env.context()
        )
        return TestClient(app)

    return build


def seed(env):
    """Three REST articles and one live one (TEST FIXTURES)."""
    job = env.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=T0 - timedelta(days=1),
        end=NOW,
        requested_by="t",
    )
    rest = [
        news_item(
            1,
            T0,
            headline="Fixture Corp Beats <b>Earnings</b> Estimates",
            summary="Strong quarter &amp; guidance.",
            symbols=["FXC"],
            url="https://example.com/1",
            source="fixturewire",
        ),
        news_item(
            2,
            T0 + timedelta(hours=1),
            headline="US CPI Rises 0.3%",
            symbols=[],
            source="otherwire",
            url="javascript:alert(1)",
        ),
        news_item(
            3,
            T0 + timedelta(hours=2),
            headline="Stocks Moving In The Session",
            symbols=["FXC", "TWO", "ZZZZ", "AAA", "BBB"],
        ),
    ]
    session_id = stream_session(env.factory, later(1))
    with session_scope(env.factory) as session:
        store = env.service.store(session)
        store.ingest(
            [
                normalize_article(
                    i, origin=ArticleOrigin.HISTORICAL_REST, received_at=NOW, store_content=False
                )
                for i in rest
            ],
            job_id=job.id,
        )
        store.ingest(
            [
                normalize_article(
                    news_item(
                        4,
                        later(1),
                        headline="Second Fixture Holdings Names New CEO",
                        symbols=["TWO"],
                    ),
                    origin=ArticleOrigin.REALTIME_STREAM,
                    received_at=later(1),
                    store_content=False,
                )
            ],
            stream_session_id=session_id,
        )
    return job


def test_feed_filters_and_sanitized_fields(env, client):
    seed(env)
    env.clock.set(later(2))
    api = client()
    body = api.get(f"{BASE}/articles").json()
    assert [a["provider_article_id"] for a in body["articles"]] == ["4", "3", "2", "1"]
    assert any("is a trading signal" in n for n in body["notes"])
    first = next(a for a in body["articles"] if a["provider_article_id"] == "1")
    assert first["headline"] == "Fixture Corp Beats Earnings Estimates"  # tags removed
    assert first["summary"] == "Strong quarter & guidance."
    assert first["first_origin"] == "HISTORICAL_REST"
    assert first["original_available_at"] is None
    assert first["event"]["primary_category"] == "EARNINGS"
    assert first["assets"][0]["status"] == "MAPPED"
    cpi = next(a for a in body["articles"] if a["provider_article_id"] == "2")
    assert cpi["url"] is None  # javascript: URLs never reach a client
    assert cpi["event"]["scope"] == "MARKET"
    live = body["articles"][0]
    assert live["first_origin"] == "REALTIME_STREAM"
    assert live["original_available_at"] is not None

    def ids(**params):
        return [
            a["provider_article_id"]
            for a in api.get(f"{BASE}/articles", params=params).json()["articles"]
        ]

    assert ids(asset_id=str(env.assets["FXC"])) == ["3", "1"]
    assert ids(symbol="two") == ["4", "3"]
    assert ids(source="otherwire") == ["2"]
    assert ids(category="INFLATION") == ["2"]
    assert ids(category="MANAGEMENT_CHANGES") == ["4"]
    assert ids(origin="REALTIME_STREAM") == ["4"]
    assert ids(scope="MARKET") == ["2"]
    assert ids(q="cpi") == ["2"]
    assert ids(
        start=(T0 + timedelta(minutes=30)).isoformat(),
        end=(T0 + timedelta(hours=1, minutes=30)).isoformat(),
    ) == ["2"]


def test_cursor_pagination_and_point_in_time(env, client):
    seed(env)
    api = client()
    page1 = api.get(f"{BASE}/articles", params={"limit": 2}).json()
    page2 = api.get(f"{BASE}/articles", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [a["provider_article_id"] for a in page1["articles"] + page2["articles"]] == [
        "4",
        "3",
        "2",
        "1",
    ]
    assert page2["next_cursor"] is None
    assert api.get(f"{BASE}/articles", params={"cursor": "garbage"}).status_code == 422
    # Before Atlas stored anything, nothing existed for it, though articles were published.
    before = api.get(f"{BASE}/articles", params={"known_at": (T0 + timedelta(hours=3)).isoformat()})
    assert before.json()["articles"] == []
    assert any("Point-in-time" in n for n in before.json()["notes"])
    # Between the backfill (NOW) and the live article (NOW + 1 min).
    mid = api.get(
        f"{BASE}/articles", params={"known_at": (NOW + timedelta(seconds=30)).isoformat()}
    )
    assert [a["provider_article_id"] for a in mid.json()["articles"]] == ["3", "2", "1"]


def test_article_detail_with_revisions(env, client):
    seed(env)
    api = client()
    article_id = api.get(f"{BASE}/articles", params={"symbol": "FXC", "limit": 1}).json()[
        "articles"
    ][0]["article_id"]
    detail = api.get(f"{BASE}/articles/{article_id}").json()
    assert detail["article"]["provider_article_id"] == "3"
    [revision] = detail["revisions"]
    assert revision["origin"] == "HISTORICAL_REST"
    assert {a["status"] for a in revision["assets"]} == {"MAPPED", "UNKNOWN_SYMBOL"}
    assert revision["events"][0]["classifier_version"] == "news-taxonomy-v1/keyword-rules-v2"
    assert revision["content"] is None
    assert "content" not in detail["provider_payload"]
    missing = api.get(f"{BASE}/articles/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


def test_asset_news_events_and_relevance(env, client):
    seed(env)
    env.clock.set(T0 + timedelta(hours=3))
    api = client()
    fxc = str(env.assets["FXC"])
    recent = api.get(f"{BASE}/assets/{fxc}").json()
    assert recent["asset"]["symbol"] == "FXC"
    assert [a["provider_article_id"] for a in recent["articles"]] == ["3", "1"]
    by_relevance = api.get(f"{BASE}/assets/{fxc}", params={"sort": "relevance"}).json()
    # The single-symbol earnings article outranks the five-symbol round-up despite its age.
    assert [a["provider_article_id"] for a in by_relevance["articles"]] == ["1", "3"]
    top = by_relevance["articles"][0]["relevance"][0]
    assert top["asset_symbol"] == "FXC"
    assert top["static_relevance"] == "1.0000"
    assert 0 < top["recency_weight"] < 1
    assert top["factors"]["version"] == "relevance-v1"
    events = api.get(f"{BASE}/assets/{fxc}/events").json()["events"]
    assert {e["event"]["primary_category"] for e in events} >= {"EARNINGS"}
    assert api.get(f"{BASE}/assets/00000000-0000-0000-0000-000000000000").status_code == 404
    all_events = api.get(f"{BASE}/events", params={"category": "EARNINGS"}).json()["events"]
    assert [e["article_id"] for e in all_events] == [
        a["article_id"] for a in recent["articles"] if a["provider_article_id"] == "1"
    ]


def test_taxonomy_summary_sources_and_calendar(env, client):
    seed(env)
    env.clock.set(later(2))
    api = client()
    taxonomy = api.get(f"{BASE}/taxonomy").json()
    assert taxonomy["classifier_version"] == "news-taxonomy-v1/keyword-rules-v2"
    assert len(taxonomy["categories"]) == 17
    assert taxonomy["confidence"]["note"].startswith("Evidence strength, not a probability")
    summary = api.get(
        f"{BASE}/summary", params={"start": (T0 - timedelta(hours=1)).isoformat()}
    ).json()
    assert summary["articles"] == 4
    assert summary["by_first_origin"] == {"HISTORICAL_REST": 3, "REALTIME_STREAM": 1}
    assert summary["by_primary_category"]["INFLATION"] == 1
    sources = {s["code"]: s for s in api.get(f"{BASE}/sources").json()}
    assert sources["fixturewire"]["articles"] == 3
    assert sources["otherwire"]["reliability"] == "NOT_ASSESSED"
    calendar = api.get(f"{BASE}/economic-calendar").json()
    assert calendar["availability"] == "NOT_CONFIGURED"
    assert calendar["events_stored"] == 0
    assert any("not an economic calendar" in n for n in calendar["notes"])


def test_jobs_endpoints_and_operator_gate(env, client):
    seed(env)
    api = client()
    jobs = api.get(f"{BASE}/ingestion-jobs").json()
    assert jobs[0]["kind"] == "HISTORICAL_BACKFILL"
    assert api.get(f"{BASE}/ingestion-jobs/{jobs[0]['id']}").json()["id"] == jobs[0]["id"]
    body = {
        "symbols": ["FXC"],
        "start": (NOW - timedelta(hours=2)).isoformat(),
        "end": NOW.isoformat(),
    }
    gated = client(operator_token="op-token-fixture-123")
    assert gated.post(f"{BASE}/ingestion-jobs", json=body).status_code == 401
    env.fake.add(news_page([news_item(50, NOW - timedelta(hours=1), symbols=["FXC"])]))
    started = gated.post(
        f"{BASE}/ingestion-jobs",
        json=body,
        headers={"X-Atlas-Operator-Token": "op-token-fixture-123"},
    )
    assert started.status_code == 202
    job = api.get(f"{BASE}/ingestion-jobs/{started.json()['id']}").json()
    assert job["status"] == "COMPLETED"  # TestClient runs background tasks before returning
    assert job["articles_new"] == 1
    bad = gated.post(
        f"{BASE}/ingestion-jobs",
        json={**body, "symbols": ["bad symbol"]},
        headers={"X-Atlas-Operator-Token": "op-token-fixture-123"},
    )
    assert bad.status_code == 422


def test_provider_and_stream_status_never_expose_credentials(env, client):
    seed(env)
    api = client()
    stream = api.get(f"{BASE}/stream").json()
    assert stream["health"] == "NOT_RUNNING"
    [provider] = api.get(f"{BASE}/providers").json()
    assert provider["provider"] == "alpaca"
    assert provider["registered"] is True
    assert provider["credentials"] == "CONFIGURED"
    assert provider["store_content"] is False
    assert provider["articles_stored"] == 4
    assert provider["capabilities"]["streaming"] is True
    assert provider["authentication"] is None
    text = json.dumps(provider) + json.dumps(stream)
    assert FAKE_KEY not in text
    assert FAKE_SECRET not in text

    with session_scope(env.factory) as session:
        session.add(
            NewsStreamStatusRecord(
                provider="alpaca",
                enabled=True,
                connection_state="SUBSCRIBED",
                auth_state="AUTHENTICATED",
                subscription_state="ACTIVE",
                requested_symbols=["*"],
                confirmed_symbols=["*"],
                heartbeat_at=env.clock.now() - timedelta(seconds=3),
                last_article_at=env.clock.now() - timedelta(minutes=20),
            )
        )
    stream = api.get(f"{BASE}/stream").json()
    assert stream["health"] == "STREAMING"
    assert stream["last_article_age_seconds"] == 1200
    env.clock.set(env.clock.now() + timedelta(minutes=5))
    assert api.get(f"{BASE}/stream").json()["health"] == "NOT_RUNNING"  # stale heartbeat
    assert api.get(f"{BASE}/stream/sessions").json()[0]["worker_id"] == "test-worker"

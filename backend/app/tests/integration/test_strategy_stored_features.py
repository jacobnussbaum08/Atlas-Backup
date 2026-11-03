"""Point-in-time news and market-intelligence features (Phase 4A) against PostgreSQL.

Articles are constructed test data stored through the real Phase 3C pipeline (fake
provider, ``tests/fixtures/alpaca_news.py``); regime rows are TEST FIXTURE rows. The point:
news is counted only where retrieval covered the window, never invented, and nothing
available after the knowledge time is used.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    IntelligenceConfigRecord,
    IntelligenceRunRecord,
    RegimeClassificationRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, FeatureRequest, FeatureStatus
from app.feature_engine.stored import StoredContext, compute_stored_features
from app.market_intelligence.enums import (
    ClassificationQuality,
    Dimension,
    KnowledgeBasis,
    RunKind,
    RunStatus,
    Scope,
)
from app.news.enums import NewsJobKind
from app.tests.fixtures.alpaca_news import news_item, news_page
from app.tests.integration.news_support import NOW, make_news_env

pytestmark = pytest.mark.integration


def news_value(env, *, cutoff, known_at, basis, name="news_event_count", **params):
    feature = REGISTRY.resolve(FeatureRequest(name, "1Day", {"lookback_hours": 24} | params))
    ctx = StoredContext(env.assets["FXC"], cutoff, known_at, basis)
    with session_scope(env.factory) as session:
        return compute_stored_features(session, [feature], ctx)[feature.key]


def backfill(env, start, end):
    job = env.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL, start=start, end=end, requested_by="test"
    )
    return asyncio.run(env.service.run_job(job.id))


@pytest.fixture
def env(fresh_engine):
    news = make_news_env(fresh_engine)
    news.fake.add(
        news_page(
            [
                news_item(1, NOW - timedelta(hours=3), symbols=["FXC"]),
                news_item(
                    2,
                    NOW - timedelta(hours=1),
                    symbols=["FXC"],
                    headline="Fixture Corp Reports Third Quarter Financial Results",
                ),
                news_item(3, NOW - timedelta(hours=30), symbols=["FXC"]),  # outside 24 h
                news_item(4, NOW - timedelta(hours=2), symbols=["TWO"]),  # another asset
            ]
        )
    )
    return news


def test_news_without_retrieval_coverage_is_unavailable_not_zero(env):
    value = news_value(env, cutoff=NOW, known_at=NOW, basis=KnowledgeBasis.RECORDED)
    assert value.status is FeatureStatus.UNAVAILABLE
    assert value.value is None
    assert "absence of articles is unknown" in (value.reason or "")


def test_covered_window_counts_the_asset_articles(env):
    backfill(env, NOW - timedelta(hours=48), NOW)
    value = news_value(env, cutoff=NOW, known_at=NOW, basis=KnowledgeBasis.RECORDED)
    assert value.status is FeatureStatus.VALID
    assert value.value == 2.0
    assert value.available_at is not None
    assert value.available_at <= NOW
    latest = news_value(
        env,
        cutoff=NOW,
        known_at=NOW,
        basis=KnowledgeBasis.RECORDED,
        name="news_hours_since_latest",
    )
    assert latest.value == pytest.approx(1.0)
    crypto = news_value(
        env, cutoff=NOW, known_at=NOW, basis=KnowledgeBasis.RECORDED, category="CRYPTOCURRENCY"
    )
    assert (crypto.status, crypto.value) == (FeatureStatus.VALID, 0.0)  # covered: truly none
    none = news_value(
        env,
        cutoff=NOW,
        known_at=NOW,
        basis=KnowledgeBasis.RECORDED,
        name="news_hours_since_latest",
        category="CRYPTOCURRENCY",
    )
    assert none.status is FeatureStatus.UNDEFINED


def test_partial_coverage_is_unavailable(env):
    backfill(env, NOW - timedelta(hours=10), NOW)  # the 24 h window is only partly covered
    value = news_value(env, cutoff=NOW, known_at=NOW, basis=KnowledgeBasis.RECORDED)
    assert value.status is FeatureStatus.UNAVAILABLE


def test_news_known_only_later_never_enters_an_earlier_decision(env):
    backfill(env, NOW - timedelta(hours=48), NOW)  # retrieved at NOW
    cutoff = NOW - timedelta(minutes=90)
    # At the cutoff Atlas had not retrieved anything: RECORDED view has no coverage.
    recorded = news_value(env, cutoff=cutoff, known_at=cutoff, basis=KnowledgeBasis.RECORDED)
    assert recorded.status is FeatureStatus.UNAVAILABLE
    # Reconstructed afterwards, only articles published by the cutoff count (1 of 2).
    rebuilt = news_value(env, cutoff=cutoff, known_at=NOW, basis=KnowledgeBasis.RECONSTRUCTED)
    assert (rebuilt.status, rebuilt.value) == (FeatureStatus.VALID, 1.0)


def _regime(session: Session, asset_id, *, as_of, available_at, basis, label="TRENDING_UP"):
    config = IntelligenceConfigRecord(
        id=uuid.uuid4(),
        code=f"fixture-{uuid.uuid4().hex[:6]}",
        version=1,
        description="TEST FIXTURE",
        config={},
        content_sha256=uuid.uuid4().hex,
        created_by="test",
    )
    session.add(config)
    session.flush()
    run = IntelligenceRunRecord(
        id=uuid.uuid4(),
        kind=RunKind.LIVE,
        status=RunStatus.COMPLETED,
        basis=basis,
        provider="alpaca",
        feed="iex",
        adjustment=PriceAdjustment.SPLIT,
        config_id=config.id,
        as_of_start=as_of,
        as_of_end=as_of,
        requested_by="test",
        started_at=as_of,
        heartbeat_at=available_at,
        completed_at=available_at,
    )
    session.add(run)
    session.flush()
    session.add(
        RegimeClassificationRecord(
            run_id=run.id,
            as_of=as_of,
            basis=basis,
            available_at=available_at,
            scope=Scope.ASSET,
            asset_id=asset_id,
            timeframe="1Day",
            dimension=Dimension.TREND,
            algorithm="fixture",
            config_id=config.id,
            label=label,
            confidence=Decimal("0.7"),
            probabilities={label: "0.7", "UNCERTAIN": "0.3"},
            quality=ClassificationQuality.OK,
        )
    )
    session.flush()


def regime_value(env, *, cutoff, basis, known_at=None, **params):
    feature = REGISTRY.resolve(FeatureRequest("regime_label", "1Day", params))
    ctx = StoredContext(env.assets["FXC"], cutoff, known_at or cutoff, basis)
    with session_scope(env.factory) as session:
        return compute_stored_features(session, [feature], ctx)[feature.key]


def test_regime_features_are_point_in_time_and_age_limited(env):
    asset = env.assets["FXC"]
    t = NOW - timedelta(days=3)
    with session_scope(env.factory) as session:
        _regime(
            session,
            asset,
            as_of=t,
            available_at=t + timedelta(minutes=5),
            basis=KnowledgeBasis.RECORDED,
            label="TRENDING_UP",
        )
        # A later reconstruction of the same decision time, computed afterwards.
        _regime(
            session,
            asset,
            as_of=t + timedelta(hours=1),
            available_at=NOW,
            basis=KnowledgeBasis.RECONSTRUCTED,
            label="SIDEWAYS",
        )
    at = t + timedelta(hours=2)
    recorded = regime_value(env, cutoff=at, basis=KnowledgeBasis.RECORDED)
    assert (recorded.status, recorded.value) == (FeatureStatus.VALID, "TRENDING_UP")
    assert recorded.available_at == t + timedelta(minutes=5)
    rebuilt = regime_value(env, cutoff=at, basis=KnowledgeBasis.RECONSTRUCTED, known_at=NOW)
    assert rebuilt.value == "SIDEWAYS"
    before = regime_value(env, cutoff=t - timedelta(minutes=1), basis=KnowledgeBasis.RECORDED)
    assert before.status is FeatureStatus.UNAVAILABLE
    stale = regime_value(
        env, cutoff=t + timedelta(hours=10), basis=KnowledgeBasis.RECORDED, max_age_hours=6
    )
    assert stale.status is FeatureStatus.UNAVAILABLE
    assert "old" in (stale.reason or "")


def period_news(env, closes, **params):
    feature = REGISTRY.resolve(FeatureRequest("period_news_count", "1Day", params))
    ctx = StoredContext(env.assets["FXC"], NOW, NOW, KnowledgeBasis.RECORDED, tuple(closes))
    with session_scope(env.factory) as session:
        return compute_stored_features(session, [feature], ctx)[feature.key]


def test_period_news_count_counts_only_articles_before_the_session_close(env):
    """Phase 4B: the event-driven strategy's window ends at the newest session close, so
    an article published after that close cannot explain the period's price reaction."""
    closes = (NOW - timedelta(hours=25), NOW - timedelta(minutes=90))  # FIXTURE closes
    assert period_news(env, closes).status is FeatureStatus.UNAVAILABLE  # no coverage yet
    backfill(env, NOW - timedelta(hours=48), NOW)
    value = period_news(env, closes)
    # Article 1 (3 h before NOW) counts; article 2 (1 h before NOW, after the close) and
    # article 3 (before the previous close) do not.
    assert (value.status, value.value) == (FeatureStatus.VALID, 1.0)
    assert value.inputs["window_end"] == closes[-1].isoformat()
    wider = period_news(env, (NOW - timedelta(hours=40), *closes), sessions=2)
    assert wider.value == 2.0  # now article 3 (30 h before NOW) is inside the window
    assert period_news(env, ()).status is FeatureStatus.UNAVAILABLE

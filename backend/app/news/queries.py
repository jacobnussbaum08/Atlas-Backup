"""Read models for news and events (Phase 3C).

Two views over the same append-only history:

* **Current** (default): each article's current revision and that revision's newest event.
* **Point in time** (``known_at``): for each article, the newest revision Atlas had
  *stored* by ``known_at`` (``available_at <= known_at``) and the newest event available by
  then. Articles first retrieved after ``known_at`` do not exist in this view, even when
  they were published before it: a backfill never leaks into the past (D-075). This is
  the view backtests and strategies must use.

Relevance in feeds combines the stored static relevance with a recency weight computed
for the query time; it orders articles and predicts nothing (``relevance.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, and_, exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.database.models import (
    Asset,
    EventAssetRelationshipRecord,
    EventClassificationRecord,
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsAssetMappingRecord,
    NewsEventRecord,
)
from app.news.enums import ArticleOrigin, EventCategory, EventScope, MappingStatus, RevisionReason
from app.news.relevance import recency_weight

R = NewsArticleRevisionRecord
A = NewsArticleRecord
E = NewsEventRecord


# ------------------------------------------------------------------ view models
class AssetLink(BaseModel):
    provider_symbol: str
    status: MappingStatus
    asset_id: uuid.UUID | None
    asset_symbol: str | None
    asset_status: str | None
    reason: str
    mapping_version: str


class CategoryView(BaseModel):
    category: EventCategory
    source: str
    method: str
    confidence: str
    verification: str
    is_primary: bool
    score: int
    provider_label: str | None
    evidence: dict[str, Any]


class RelevanceView(BaseModel):
    asset_id: uuid.UUID
    asset_symbol: str | None
    version: str
    static_relevance: str
    recency_weight: float
    combined: float
    factors: dict[str, Any]


class EventView(BaseModel):
    event_id: int
    primary_category: EventCategory
    scope: EventScope
    taxonomy_version: str
    classifier_version: str
    rules_sha256: str
    event_time: datetime
    available_at: datetime
    categories: list[CategoryView]


class ArticleSummary(BaseModel):
    article_id: uuid.UUID
    provider: str
    provider_article_id: str
    revision: int
    current_revision: int
    headline: str
    summary: str | None
    author: str | None
    source: str | None
    url: str | None
    symbols: list[str]
    published_at: datetime
    provider_updated_at: datetime | None
    first_received_at: datetime
    first_available_at: datetime
    first_origin: ArticleOrigin
    first_live_received_at: datetime | None
    revision_origin: ArticleOrigin
    revision_received_at: datetime
    revision_available_at: datetime
    original_available_at: datetime | None
    assets: list[AssetLink]
    event: EventView | None
    relevance: list[RelevanceView]


class RevisionView(BaseModel):
    revision: int
    reason: str
    origin: ArticleOrigin
    headline: str
    summary: str | None
    author: str | None
    source: str | None
    url: str | None
    symbols: list[str]
    published_at: datetime
    provider_updated_at: datetime | None
    received_at: datetime
    processed_at: datetime
    available_at: datetime
    original_available_at: datetime | None
    content_sha256: str
    has_body: bool
    body_sha256: str | None
    content: str | None
    images: list[dict[str, Any]]
    provider_categories: list[str]
    ingestion_job_id: uuid.UUID | None
    stream_session_id: uuid.UUID | None
    assets: list[AssetLink]
    events: list[EventView]


class ArticleDetail(BaseModel):
    article: ArticleSummary
    observation_count: int
    last_seen_at: datetime
    revisions: list[RevisionView]
    provider_payload: dict[str, Any]


@dataclass(frozen=True)
class FeedFilter:
    asset_id: uuid.UUID | None = None
    symbol: str | None = None
    source: str | None = None
    category: EventCategory | None = None
    scope: EventScope | None = None
    origin: ArticleOrigin | None = None
    start: datetime | None = None
    end: datetime | None = None
    search: str | None = None
    known_at: datetime | None = None


# ------------------------------------------------------------------ queries
def _chosen(known_at: datetime | None) -> Any:
    if known_at is None:
        return (
            select(A.id.label("article_id"), R.id.label("revision_id"))
            .join(R, and_(R.article_id == A.id, R.revision == A.current_revision))
            .subquery("chosen")
        )
    return (
        select(R.article_id.label("article_id"), R.id.label("revision_id"))
        .where(R.available_at <= known_at, R.reason != RevisionReason.LATE_EARLIER_VERSION)
        .ext(distinct_on(R.article_id))
        .order_by(R.article_id, R.revision.desc())
        .subquery("chosen")
    )


def _feed_statement(
    f: FeedFilter,
) -> Select[NewsArticleRecord, NewsArticleRevisionRecord, NewsEventRecord]:
    chosen = _chosen(f.known_at)
    event_id = select(E.id).where(E.revision_id == chosen.c.revision_id)
    if f.known_at is not None:
        event_id = event_id.where(E.available_at <= f.known_at)
    event_subq = event_id.order_by(E.id.desc()).limit(1).correlate(chosen).scalar_subquery()
    stmt = (
        select(A, R, E)
        .join(chosen, chosen.c.article_id == A.id)
        .join(R, R.id == chosen.c.revision_id)
        .outerjoin(E, E.id == event_subq)
    )
    if f.asset_id is not None:
        stmt = stmt.where(
            exists().where(
                NewsAssetMappingRecord.revision_id == R.id,
                NewsAssetMappingRecord.asset_id == f.asset_id,
                NewsAssetMappingRecord.status == MappingStatus.MAPPED,
            )
        )
    if f.symbol:
        stmt = stmt.where(R.symbols.contains([f.symbol.strip().upper()]))
    if f.source:
        stmt = stmt.where(R.source_code == f.source.strip().lower())
    if f.category is not None:
        stmt = stmt.where(
            exists().where(
                EventClassificationRecord.event_id == E.id,
                EventClassificationRecord.category == f.category,
            )
        )
    if f.scope is not None:
        stmt = stmt.where(E.scope == f.scope)
    if f.origin is not None:
        stmt = stmt.where(A.first_origin == f.origin)
    if f.start is not None:
        stmt = stmt.where(R.published_at >= f.start)
    if f.end is not None:
        stmt = stmt.where(R.published_at <= f.end)
    if f.search:
        escaped = f.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(R.headline.ilike(f"%{escaped}%", escape="\\"))
    return stmt


def parse_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    stamp, _, ident = cursor.partition("|")
    return datetime.fromisoformat(stamp), uuid.UUID(ident)


def make_cursor(published_at: datetime, article_id: uuid.UUID) -> str:
    return f"{published_at.isoformat()}|{article_id}"


def feed(
    session: Session,
    f: FeedFilter,
    *,
    at: datetime,
    half_life_hours: float,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[ArticleSummary], str | None]:
    stmt = _feed_statement(f)
    if cursor:
        published, ident = parse_cursor(cursor)
        stmt = stmt.where(tuple_(R.published_at, A.id) < tuple_(published, ident))
    rows = session.execute(stmt.order_by(R.published_at.desc(), A.id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    summaries = _summaries(session, rows, at=at, half_life_hours=half_life_hours)
    next_cursor = (
        make_cursor(summaries[-1].published_at, summaries[-1].article_id) if more else None
    )
    return summaries, next_cursor


def _summaries(
    session: Session, rows: Sequence[Any], *, at: datetime, half_life_hours: float
) -> list[ArticleSummary]:
    revision_ids = [row[1].id for row in rows]
    event_ids = [row[2].id for row in rows if row[2] is not None]
    links = asset_links(session, revision_ids)
    categories = category_views(session, event_ids)
    relevance = relevance_views(session, event_ids, at=at, half_life_hours=half_life_hours)
    out: list[ArticleSummary] = []
    for article, revision, event in rows:
        out.append(
            summary_view(
                article,
                revision,
                event_view(event, categories.get(event.id, [])) if event is not None else None,
                links.get(revision.id, []),
                relevance.get(event.id, []) if event is not None else [],
            )
        )
    return out


def summary_view(
    article: NewsArticleRecord,
    revision: NewsArticleRevisionRecord,
    event: EventView | None,
    links: list[AssetLink],
    relevance: list[RelevanceView],
) -> ArticleSummary:
    return ArticleSummary(
        article_id=article.id,
        provider=article.provider,
        provider_article_id=article.provider_article_id,
        revision=revision.revision,
        current_revision=article.current_revision,
        headline=revision.headline,
        summary=revision.summary,
        author=revision.author,
        source=revision.source_code,
        url=revision.url,
        symbols=list(revision.symbols),
        published_at=revision.published_at,
        provider_updated_at=revision.provider_updated_at,
        first_received_at=article.first_received_at,
        first_available_at=article.first_available_at,
        first_origin=article.first_origin,
        first_live_received_at=article.first_live_received_at,
        revision_origin=revision.origin,
        revision_received_at=revision.received_at,
        revision_available_at=revision.available_at,
        original_available_at=revision.original_available_at,
        assets=links,
        event=event,
        relevance=relevance,
    )


def asset_links(session: Session, revision_ids: Sequence[int]) -> dict[int, list[AssetLink]]:
    if not revision_ids:
        return {}
    M = NewsAssetMappingRecord
    rows = session.execute(
        select(M, Asset.symbol)
        .outerjoin(Asset, Asset.id == M.asset_id)
        .where(M.revision_id.in_(revision_ids))
        .order_by(M.revision_id, M.id)
    ).all()
    out: dict[int, list[AssetLink]] = {}
    for mapping, symbol in rows:
        out.setdefault(mapping.revision_id, []).append(
            AssetLink(
                provider_symbol=mapping.provider_symbol,
                status=mapping.status,
                asset_id=mapping.asset_id,
                asset_symbol=symbol,
                asset_status=mapping.asset_status,
                reason=mapping.reason,
                mapping_version=mapping.mapping_version,
            )
        )
    return out


def category_views(session: Session, event_ids: Sequence[int]) -> dict[int, list[CategoryView]]:
    if not event_ids:
        return {}
    C = EventClassificationRecord
    rows = session.scalars(
        select(C)
        .where(C.event_id.in_(event_ids))
        .order_by(C.event_id, C.is_primary.desc(), C.score.desc(), C.id)
    ).all()
    out: dict[int, list[CategoryView]] = {}
    for c in rows:
        out.setdefault(c.event_id, []).append(
            CategoryView(
                category=c.category,
                source=c.source.value,
                method=c.method.value,
                confidence=c.confidence.value,
                verification=c.verification.value,
                is_primary=c.is_primary,
                score=c.score,
                provider_label=c.provider_label,
                evidence=c.evidence,
            )
        )
    return out


def relevance_views(
    session: Session, event_ids: Sequence[int], *, at: datetime, half_life_hours: float
) -> dict[int, list[RelevanceView]]:
    if not event_ids:
        return {}
    X = EventAssetRelationshipRecord
    rows = session.execute(
        select(X, Asset.symbol)
        .join(Asset, Asset.id == X.asset_id)
        .where(X.event_id.in_(event_ids))
        .order_by(X.event_id, X.static_relevance.desc(), X.id)
    ).all()
    out: dict[int, list[RelevanceView]] = {}
    for rel, symbol in rows:
        weight = recency_weight(rel.event_time, at, half_life_hours)
        out.setdefault(rel.event_id, []).append(
            RelevanceView(
                asset_id=rel.asset_id,
                asset_symbol=symbol,
                version=rel.relevance_version,
                static_relevance=str(rel.static_relevance),
                recency_weight=round(weight, 6),
                combined=round(float(rel.static_relevance) * weight, 6),
                factors=rel.relevance_factors,
            )
        )
    return out


def event_view(event: NewsEventRecord, categories: list[CategoryView]) -> EventView:
    return EventView(
        event_id=event.id,
        primary_category=event.primary_category,
        scope=event.scope,
        taxonomy_version=event.taxonomy_version,
        classifier_version=event.classifier_version,
        rules_sha256=event.rules_sha256,
        event_time=event.event_time,
        available_at=event.available_at,
        categories=categories,
    )


def article_detail(
    session: Session, article_id: uuid.UUID, *, at: datetime, half_life_hours: float
) -> ArticleDetail | None:
    article = session.get(A, article_id)
    if article is None:
        return None
    revisions = list(
        session.scalars(select(R).where(R.article_id == article_id).order_by(R.revision)).all()
    )
    events = list(
        session.scalars(
            select(E).where(E.article_id == article_id).order_by(E.revision_id, E.id)
        ).all()
    )
    links = asset_links(session, [r.id for r in revisions])
    categories = category_views(session, [e.id for e in events])
    relevance = relevance_views(
        session, [e.id for e in events], at=at, half_life_hours=half_life_hours
    )
    events_by_revision: dict[int, list[NewsEventRecord]] = {}
    for e in events:
        events_by_revision.setdefault(e.revision_id, []).append(e)
    current = next(r for r in revisions if r.revision == article.current_revision)
    current_events = events_by_revision.get(current.id, [])
    current_event = current_events[-1] if current_events else None
    summary = summary_view(
        article,
        current,
        event_view(current_event, categories.get(current_event.id, []))
        if current_event is not None
        else None,
        links.get(current.id, []),
        relevance.get(current_event.id, []) if current_event is not None else [],
    )
    return ArticleDetail(
        article=summary,
        observation_count=article.observation_count,
        last_seen_at=article.last_seen_at,
        provider_payload=current.provider_payload,
        revisions=[
            RevisionView(
                revision=r.revision,
                reason=r.reason.value,
                origin=r.origin,
                headline=r.headline,
                summary=r.summary,
                author=r.author,
                source=r.source_code,
                url=r.url,
                symbols=list(r.symbols),
                published_at=r.published_at,
                provider_updated_at=r.provider_updated_at,
                received_at=r.received_at,
                processed_at=r.processed_at,
                available_at=r.available_at,
                original_available_at=r.original_available_at,
                content_sha256=r.content_sha256,
                has_body=r.has_body,
                body_sha256=r.body_sha256,
                content=r.content,
                images=r.images,
                provider_categories=list(r.provider_categories),
                ingestion_job_id=r.ingestion_job_id,
                stream_session_id=r.stream_session_id,
                assets=links.get(r.id, []),
                events=[
                    event_view(e, categories.get(e.id, []))
                    for e in events_by_revision.get(r.id, [])
                ],
            )
            for r in revisions
        ],
    )


def category_counts(session: Session, f: FeedFilter) -> dict[str, int]:
    """Primary-category counts over the filtered articles (event-less articles excluded)."""
    sub = _feed_statement(f).with_only_columns(E.primary_category).subquery()
    rows = session.execute(
        select(sub.c.primary_category, func.count()).group_by(sub.c.primary_category)
    ).all()
    return {str(category): int(count) for category, count in rows if category is not None}


def origin_counts(session: Session, f: FeedFilter) -> dict[str, int]:
    sub = _feed_statement(f).with_only_columns(A.first_origin).subquery()
    rows = session.execute(
        select(sub.c.first_origin, func.count()).group_by(sub.c.first_origin)
    ).all()
    return {str(origin): int(count) for origin, count in rows}


__all__ = [
    "ArticleDetail",
    "ArticleSummary",
    "AssetLink",
    "CategoryView",
    "EventView",
    "FeedFilter",
    "RelevanceView",
    "RevisionView",
    "article_detail",
    "category_counts",
    "feed",
    "origin_counts",
]

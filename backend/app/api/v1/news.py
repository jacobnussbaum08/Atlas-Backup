"""News and event intelligence API (Phase 3C).

Read endpoints serve what Atlas stored. ``known_at`` turns a read into the point-in-time
view (only revisions and events available by then, D-075): use it for anything that
simulates the past. Starting a historical retrieval is an operational action (operator
token, as for market data). No endpoint exposes credentials or article bodies unless
their retention is enabled, and nothing here is a trading signal: classifications are
keyword estimates and relevance is an ordering heuristic.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.v1.market_data import QueryTime, require_operator
from app.core.clocks import ensure_utc
from app.core.exceptions import DependencyUnavailableError, InvalidRequestError, NotFoundError
from app.data_engine.schemas import ConnectionState
from app.database.models import (
    Asset,
    EconomicEventRecord,
    NewsArticleRecord,
    NewsIngestionJobRecord,
    NewsProviderRecord,
    NewsSourceRecord,
    NewsStreamSessionRecord,
    NewsStreamStatusRecord,
)
from app.database.sessions import session_scope
from app.news import queries
from app.news.classification import (
    CLASSIFIER_VERSION,
    RULES_SHA256,
    RULES_VERSION,
    TAXONOMY_VERSION,
    taxonomy,
)
from app.news.economic_calendar import calendar_status
from app.news.enums import (
    ArticleOrigin,
    EventCategory,
    EventScope,
    NewsJobKind,
    NewsJobStatus,
)
from app.news.ingestion import run_job_blocking
from app.news.mapping import MAPPING_VERSION
from app.news.relevance import (
    DIRECT_WEIGHT,
    HEADLINE_WEIGHT,
    RELEVANCE_VERSION,
    SPECIFICITY_WEIGHT,
)
from app.news.runtime import NewsContext

router = APIRouter(prefix="/news", tags=["news"])
_lock = threading.Lock()

DISCLAIMER = (
    "News is supporting information only. Categories are rule-based keyword estimates "
    "(unverified), relevance orders articles by how directly they concern an asset and how "
    "recent they are; neither measures sentiment, importance or price impact, and nothing "
    "here is a trading signal."
)


def news(request: Request) -> NewsContext:
    state = request.app.state
    context: NewsContext | None = getattr(state, "news", None)
    if context is None:
        factory = getattr(state, "news_factory", None)
        if factory is None:
            raise DependencyUnavailableError("news is not configured on this server")
        with _lock:
            context = getattr(state, "news", None)
            if context is None:
                context = factory()
                state.news = context
    return context


Context = Annotated[NewsContext, Depends(news)]
Limit = Annotated[int, Query(ge=1, le=200)]


# ------------------------------------------------------------------ models
class FeedResponse(BaseModel):
    articles: list[queries.ArticleSummary]
    next_cursor: str | None
    known_at: datetime | None
    as_of: datetime
    notes: list[str]


class AssetRef(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    name: str | None
    status: str


class AssetNewsResponse(BaseModel):
    asset: AssetRef
    sort: str
    articles: list[queries.ArticleSummary]
    next_cursor: str | None
    known_at: datetime | None
    as_of: datetime
    notes: list[str]


class EventItem(BaseModel):
    event: queries.EventView
    article_id: uuid.UUID
    headline: str
    source: str | None
    url: str | None
    published_at: datetime
    first_origin: ArticleOrigin
    assets: list[queries.AssetLink]
    relevance: list[queries.RelevanceView]


class EventsResponse(BaseModel):
    events: list[EventItem]
    next_cursor: str | None
    known_at: datetime | None
    as_of: datetime
    notes: list[str]


class Job(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    kind: NewsJobKind
    status: NewsJobStatus
    symbols: list[str]
    range_start: datetime
    range_end: datetime
    include_content: bool
    requested_by: str
    stream_session_id: uuid.UUID | None
    params: dict[str, Any]
    pages_retrieved: int
    articles_received: int
    articles_new: int
    revisions_new: int
    duplicates: int
    malformed: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None


class JobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=list, description="Provider symbols; empty = all")
    start: Annotated[datetime, AfterValidator(ensure_utc)]
    end: Annotated[datetime, AfterValidator(ensure_utc)]


class Source(BaseModel):
    provider: str
    code: str
    display_name: str
    reliability: str
    first_seen_at: datetime
    last_seen_at: datetime
    articles: int


class StreamSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    worker_id: str
    attempt: int
    state: str
    requested_symbols: list[str]
    confirmed_symbols: list[str]
    started_at: datetime
    authenticated_at: datetime | None
    subscribed_at: datetime | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    last_article_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    counters: dict[str, Any]


class StreamHealth(BaseModel):
    provider: str
    health: Literal[
        "NOT_RUNNING", "DISABLED", "STOPPED", "FAILED", "CONNECTING", "DEGRADED", "STREAMING"
    ]
    reasons: list[str]
    enabled: bool | None
    connection_state: str | None
    auth_state: str | None
    subscription_state: str | None
    requested_symbols: list[str]
    confirmed_symbols: list[str]
    worker_id: str | None
    worker_started_at: datetime | None
    heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    last_subscribed_at: datetime | None
    last_message_at: datetime | None
    last_article_at: datetime | None
    last_article_age_seconds: float | None
    last_persisted_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    recent_errors: list[dict[str, Any]]
    reconnect_count: int
    counters: dict[str, Any]
    recovery_jobs: list[Job]
    notes: list[str]


class ProviderStatus(BaseModel):
    provider: str
    display_name: str
    registered: bool
    credentials: str
    authentication: dict[str, Any] | None
    capabilities: dict[str, Any]
    content_policy: str
    store_content: bool
    articles_stored: int
    newest_article_published_at: datetime | None
    newest_article_received_at: datetime | None
    last_job: Job | None
    last_failed_job: Job | None
    stream_health: str
    notes: list[str]


class CalendarStatusResponse(BaseModel):
    availability: str
    provider: str | None
    reason: str
    notes: list[str]
    events_stored: int


class SummaryResponse(BaseModel):
    start: datetime
    end: datetime
    articles: int
    by_primary_category: dict[str, int]
    by_first_origin: dict[str, int]
    notes: list[str]


# ------------------------------------------------------------------ helpers
def _filter(
    *,
    asset_id: uuid.UUID | None = None,
    symbol: str | None = None,
    source: str | None = None,
    category: EventCategory | None = None,
    scope: EventScope | None = None,
    origin: ArticleOrigin | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
    known_at: datetime | None = None,
) -> queries.FeedFilter:
    if start is not None and end is not None and end < start:
        raise InvalidRequestError("end must not be before start")
    return queries.FeedFilter(
        asset_id=asset_id,
        symbol=symbol,
        source=source,
        category=category,
        scope=scope,
        origin=origin,
        start=start,
        end=end,
        search=q.strip()[:200] if q else None,
        known_at=known_at,
    )


def _notes(known_at: datetime | None) -> list[str]:
    notes = [DISCLAIMER]
    if known_at is not None:
        notes.append(
            "Point-in-time view: only article revisions and events Atlas had stored by "
            "known_at. Articles retrieved later are absent even if published earlier."
        )
    else:
        notes.append(
            "Current view: each article's newest stored revision. Articles marked "
            "HISTORICAL_REST were retrieved after publication; their availability to Atlas "
            "is the retrieval time, not the publication time."
        )
    return notes


def _cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        queries.parse_cursor(cursor)
    except ValueError:
        raise InvalidRequestError("invalid cursor") from None
    return cursor


def _asset(session: Any, asset_id: uuid.UUID) -> AssetRef:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise NotFoundError(f"asset {asset_id} does not exist")
    return AssetRef(asset_id=asset.id, symbol=asset.symbol, name=asset.name, status=asset.status)


def _event_items(articles: list[queries.ArticleSummary]) -> list[EventItem]:
    return [
        EventItem(
            event=a.event,
            article_id=a.article_id,
            headline=a.headline,
            source=a.source,
            url=a.url,
            published_at=a.published_at,
            first_origin=a.first_origin,
            assets=a.assets,
            relevance=a.relevance,
        )
        for a in articles
        if a.event is not None
    ]


# ------------------------------------------------------------------ feed and articles
@router.get("/articles", response_model=FeedResponse)
def list_articles(
    ctx: Context,
    asset_id: uuid.UUID | None = None,
    symbol: Annotated[str | None, Query(max_length=32)] = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    category: EventCategory | None = None,
    scope: EventScope | None = None,
    origin: ArticleOrigin | None = None,
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Headline contains")] = None,
    known_at: QueryTime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> FeedResponse:
    """The news feed, newest publication first, with filters."""
    f = _filter(
        asset_id=asset_id,
        symbol=symbol,
        source=source,
        category=category,
        scope=scope,
        origin=origin,
        start=start,
        end=end,
        q=q,
        known_at=known_at,
    )
    at = known_at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        articles, next_cursor = queries.feed(
            session,
            f,
            at=at,
            half_life_hours=ctx.settings.relevance_half_life_hours,
            limit=limit,
            cursor=_cursor(cursor),
        )
    return FeedResponse(
        articles=articles,
        next_cursor=next_cursor,
        known_at=known_at,
        as_of=at,
        notes=_notes(known_at),
    )


@router.get("/articles/{article_id}", response_model=queries.ArticleDetail)
def get_article(ctx: Context, article_id: uuid.UUID) -> queries.ArticleDetail:
    """One article with every stored revision, its asset mappings and classifications."""
    with session_scope(ctx.session_factory) as session:
        detail = queries.article_detail(
            session,
            article_id,
            at=ctx.clock.now(),
            half_life_hours=ctx.settings.relevance_half_life_hours,
        )
    if detail is None:
        raise NotFoundError(f"article {article_id} does not exist")
    return detail


@router.get("/assets/{asset_id}", response_model=AssetNewsResponse)
def asset_news(
    ctx: Context,
    asset_id: uuid.UUID,
    sort: Literal["recent", "relevance"] = "recent",
    category: EventCategory | None = None,
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    known_at: QueryTime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> AssetNewsResponse:
    """Articles mapped to one asset (effective-dated mapping at publication), with relevance.

    ``sort=relevance`` orders the page by static relevance x recency for this asset.
    """
    at = known_at or ctx.clock.now()
    f = _filter(asset_id=asset_id, category=category, start=start, end=end, known_at=known_at)
    with session_scope(ctx.session_factory) as session:
        asset = _asset(session, asset_id)
        articles, next_cursor = queries.feed(
            session,
            f,
            at=at,
            half_life_hours=ctx.settings.relevance_half_life_hours,
            limit=limit,
            cursor=_cursor(cursor),
        )
    for article in articles:
        article.relevance = [r for r in article.relevance if r.asset_id == asset_id]
    if sort == "relevance":
        articles.sort(
            key=lambda a: (a.relevance[0].combined if a.relevance else 0.0, a.published_at),
            reverse=True,
        )
    notes = _notes(known_at)
    notes.append(
        f"Relevance ({RELEVANCE_VERSION}) = static relevance x recency weight "
        f"(half-life {ctx.settings.relevance_half_life_hours:g} h); an ordering heuristic only."
    )
    return AssetNewsResponse(
        asset=asset,
        sort=sort,
        articles=articles,
        next_cursor=next_cursor,
        known_at=known_at,
        as_of=at,
        notes=notes,
    )


# ------------------------------------------------------------------ events and taxonomy
@router.get("/events", response_model=EventsResponse)
def list_events(
    ctx: Context,
    category: EventCategory | None = None,
    scope: EventScope | None = None,
    asset_id: uuid.UUID | None = None,
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    known_at: QueryTime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> EventsResponse:
    """Classified events (one per article's current or point-in-time revision)."""
    f = _filter(
        asset_id=asset_id,
        category=category,
        scope=scope,
        start=start,
        end=end,
        known_at=known_at,
    )
    at = known_at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        articles, next_cursor = queries.feed(
            session,
            f,
            at=at,
            half_life_hours=ctx.settings.relevance_half_life_hours,
            limit=limit,
            cursor=_cursor(cursor),
        )
    return EventsResponse(
        events=_event_items(articles),
        next_cursor=next_cursor,
        known_at=known_at,
        as_of=at,
        notes=_notes(known_at),
    )


@router.get("/assets/{asset_id}/events", response_model=EventsResponse)
def asset_events(
    ctx: Context,
    asset_id: uuid.UUID,
    category: EventCategory | None = None,
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    known_at: QueryTime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> EventsResponse:
    """Events associated with one asset."""
    f = _filter(asset_id=asset_id, category=category, start=start, end=end, known_at=known_at)
    at = known_at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        _asset(session, asset_id)
        articles, next_cursor = queries.feed(
            session,
            f,
            at=at,
            half_life_hours=ctx.settings.relevance_half_life_hours,
            limit=limit,
            cursor=_cursor(cursor),
        )
    for article in articles:
        article.relevance = [r for r in article.relevance if r.asset_id == asset_id]
    return EventsResponse(
        events=_event_items(articles),
        next_cursor=next_cursor,
        known_at=known_at,
        as_of=at,
        notes=_notes(known_at),
    )


@router.get("/taxonomy")
def get_taxonomy(ctx: Context) -> dict[str, Any]:
    """The event taxonomy, its rules and the mapping and relevance methodologies."""
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "rules_version": RULES_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "rules_sha256": RULES_SHA256,
        "categories": taxonomy(),
        "scoring": {
            "headline_match": 2,
            "summary_match": 1,
            "assigned_when_score_at_least": 1,
            "primary": "provider label where known, else highest score, ties by taxonomy order",
        },
        "confidence": {
            "MEDIUM": "matched in the headline, by symbol rule, or by two or more summary rules",
            "LOW": "a single summary match",
            "NONE": "nothing matched (OTHER)",
            "note": "Evidence strength, not a probability. Atlas labels are UNVERIFIED.",
        },
        "mapping": {
            "version": MAPPING_VERSION,
            "method": "provider symbol resolved with effective-dated asset_identifiers at the "
            "article's publication time; ambiguous or not-yet-effective symbols are not forced",
            "ambiguity_window_hours": ctx.settings.mapping_ambiguity_window_hours,
        },
        "relevance": {
            "version": RELEVANCE_VERSION,
            "static": {
                "direct": str(DIRECT_WEIGHT),
                "specificity": str(SPECIFICITY_WEIGHT),
                "headline_mention": str(HEADLINE_WEIGHT),
                "source": "0 (publishers not assessed)",
            },
            "recency_half_life_hours": ctx.settings.relevance_half_life_hours,
        },
        "notes": [DISCLAIMER],
    }


@router.get("/summary", response_model=SummaryResponse)
def summary(
    ctx: Context,
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    asset_id: uuid.UUID | None = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
) -> SummaryResponse:
    """Article counts by primary category and first origin over a publication window."""
    end = end or ctx.clock.now()
    start = start or end - timedelta(days=1)
    f = _filter(asset_id=asset_id, source=source, start=start, end=end)
    with session_scope(ctx.session_factory) as session:
        by_category = queries.category_counts(session, f)
        by_origin = queries.origin_counts(session, f)
    return SummaryResponse(
        start=start,
        end=end,
        articles=sum(by_origin.values()),
        by_primary_category=by_category,
        by_first_origin=by_origin,
        notes=[DISCLAIMER],
    )


@router.get("/sources", response_model=list[Source])
def list_sources(ctx: Context) -> list[Source]:
    with session_scope(ctx.session_factory) as session:
        counts = dict(
            session.execute(
                select(NewsArticleRecord.source_id, func.count()).group_by(
                    NewsArticleRecord.source_id
                )
            ).all()
        )
        rows = session.scalars(select(NewsSourceRecord).order_by(NewsSourceRecord.code)).all()
        return [
            Source(
                provider=s.provider,
                code=s.code,
                display_name=s.display_name,
                reliability=s.reliability.value,
                first_seen_at=s.first_seen_at,
                last_seen_at=s.last_seen_at,
                articles=int(counts.get(s.id, 0)),
            )
            for s in rows
        ]


# ------------------------------------------------------------------ ingestion jobs
@router.get("/ingestion-jobs", response_model=list[Job])
def list_jobs(ctx: Context, kind: NewsJobKind | None = None, limit: Limit = 50) -> list[Job]:
    return [Job.model_validate(j) for j in ctx.service.list_jobs(limit=limit, kind=kind)]


@router.get("/ingestion-jobs/{job_id}", response_model=Job)
def get_job(ctx: Context, job_id: uuid.UUID) -> Job:
    return Job.model_validate(ctx.service.get_job(job_id))


@router.post(
    "/ingestion-jobs",
    response_model=Job,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_operator)],
)
def start_job(ctx: Context, body: JobBody, background: BackgroundTasks) -> Job:
    """Start a historical news retrieval (operator token required). Runs in the background;
    poll the job. Articles stored this way are labelled as retrieved later."""
    job = ctx.service.create_job(
        kind=NewsJobKind.HISTORICAL_BACKFILL,
        start=body.start,
        end=body.end,
        symbols=body.symbols,
        requested_by="api",
    )
    background.add_task(run_job_blocking, ctx.service, job.id)
    return Job.model_validate(job)


# ------------------------------------------------------------------ provider and stream
def _stream_health(
    row: NewsStreamStatusRecord | None, now: datetime, stale_after: timedelta
) -> tuple[str, list[str]]:
    if row is None:
        return "NOT_RUNNING", ["no news stream worker has reported yet"]
    if not row.enabled:
        return "DISABLED", ["NEWS_STREAM_ENABLED is false"]
    state = row.connection_state
    if state == ConnectionState.STOPPED.value:
        return "STOPPED", ["the worker stopped (graceful shutdown)"]
    if now - row.heartbeat_at > stale_after:
        return "NOT_RUNNING", [
            f"no heartbeat for {int((now - row.heartbeat_at).total_seconds())} s: the worker "
            "is not running"
        ]
    if state == ConnectionState.FAILED.value:
        return "FAILED", [row.last_error or "permanent failure; not retrying"]
    if state == ConnectionState.SUBSCRIBED.value:
        if row.subscription_state == "ACTIVE":
            return "STREAMING", ["authenticated and subscription confirmed"]
        return "DEGRADED", [f"subscription {row.subscription_state.lower()}"]
    return "CONNECTING", [f"connection {state.lower()}"]


@router.get("/stream", response_model=StreamHealth)
def stream_health(ctx: Context) -> StreamHealth:
    """Health of the real-time news worker (written by it; this API opens no connection)."""
    now = ctx.clock.now()
    provider = ctx.provider.provider_id
    with session_scope(ctx.session_factory) as session:
        row = session.get(NewsStreamStatusRecord, provider)
        jobs = session.scalars(
            select(NewsIngestionJobRecord)
            .where(NewsIngestionJobRecord.kind == NewsJobKind.STREAM_RECOVERY)
            .order_by(NewsIngestionJobRecord.created_at.desc())
            .limit(5)
        ).all()
        recovery = [Job.model_validate(j) for j in jobs]
    health, reasons = _stream_health(row, now, timedelta(seconds=ctx.settings.stream_stale_after))

    def age(at: datetime | None) -> float | None:
        return round((now - at).total_seconds(), 3) if at is not None else None

    return StreamHealth(
        provider=provider,
        health=health,
        reasons=reasons,
        enabled=row.enabled if row else None,
        connection_state=row.connection_state if row else None,
        auth_state=row.auth_state if row else None,
        subscription_state=row.subscription_state if row else None,
        requested_symbols=list(row.requested_symbols) if row else [],
        confirmed_symbols=list(row.confirmed_symbols) if row else [],
        worker_id=row.worker_id if row else None,
        worker_started_at=row.worker_started_at if row else None,
        heartbeat_at=row.heartbeat_at if row else None,
        heartbeat_age_seconds=age(row.heartbeat_at) if row else None,
        last_subscribed_at=row.last_subscribed_at if row else None,
        last_message_at=row.last_message_at if row else None,
        last_article_at=row.last_article_at if row else None,
        last_article_age_seconds=age(row.last_article_at) if row else None,
        last_persisted_at=row.last_persisted_at if row else None,
        last_error=row.last_error if row else None,
        last_error_at=row.last_error_at if row else None,
        recent_errors=list(row.recent_errors) if row else [],
        reconnect_count=row.reconnect_count if row else 0,
        counters=dict(row.counters) if row else {},
        recovery_jobs=recovery,
        notes=[
            "News arrives irregularly: minutes without articles are normal and are not an "
            "outage. Connection health comes from the worker's heartbeat and subscription.",
            "After every (re)connection the worker retrieves an overlapping window over REST "
            "(recovery jobs); duplicates are recognised and not stored twice.",
        ],
    )


@router.get("/stream/sessions", response_model=list[StreamSession])
def stream_sessions(ctx: Context, limit: Limit = 20) -> list[StreamSession]:
    with session_scope(ctx.session_factory) as session:
        rows = session.scalars(
            select(NewsStreamSessionRecord)
            .where(NewsStreamSessionRecord.provider == ctx.provider.provider_id)
            .order_by(NewsStreamSessionRecord.started_at.desc())
            .limit(limit)
        ).all()
        return [StreamSession.model_validate(r) for r in rows]


@router.get("/providers", response_model=list[ProviderStatus])
async def providers(
    ctx: Context,
    verify: Annotated[bool, Query(description="Check credentials with one request")] = False,
) -> list[ProviderStatus]:
    """News provider status: credentials, capabilities, content policy and recent outcomes."""
    provider = ctx.provider
    authentication = None
    if verify:
        authentication = (await provider.check_authentication()).model_dump(mode="json")
    return [await asyncio.to_thread(_provider_status, ctx, authentication)]


def _provider_status(ctx: NewsContext, authentication: dict[str, Any] | None) -> ProviderStatus:
    provider = ctx.provider
    caps = provider.capabilities()
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        registered = session.get(NewsProviderRecord, provider.provider_id) is not None
        count, newest_published, newest_received = session.execute(
            select(
                func.count(),
                func.max(NewsArticleRecord.published_at),
                func.max(NewsArticleRecord.first_received_at),
            ).where(NewsArticleRecord.provider == provider.provider_id)
        ).one()
        last = session.scalars(
            select(NewsIngestionJobRecord)
            .where(NewsIngestionJobRecord.provider == provider.provider_id)
            .order_by(NewsIngestionJobRecord.created_at.desc())
            .limit(1)
        ).one_or_none()
        failed = session.scalars(
            select(NewsIngestionJobRecord)
            .where(
                NewsIngestionJobRecord.provider == provider.provider_id,
                NewsIngestionJobRecord.status == NewsJobStatus.FAILED,
            )
            .order_by(NewsIngestionJobRecord.created_at.desc())
            .limit(1)
        ).one_or_none()
        row = session.get(NewsStreamStatusRecord, provider.provider_id)
        last_job = Job.model_validate(last) if last else None
        last_failed = Job.model_validate(failed) if failed else None
    health, _ = _stream_health(row, now, timedelta(seconds=ctx.settings.stream_stale_after))
    return ProviderStatus(
        provider=provider.provider_id,
        display_name=provider.display_name,
        registered=registered,
        credentials=provider.credential_status().value,
        authentication=authentication,
        capabilities=caps.model_dump(mode="json"),
        content_policy=caps.content_policy,
        store_content=ctx.settings.store_content,
        articles_stored=int(count),
        newest_article_published_at=newest_published,
        newest_article_received_at=newest_received,
        last_job=last_job,
        last_failed_job=last_failed,
        stream_health=health,
        notes=list(caps.notes),
    )


@router.get("/economic-calendar", response_model=CalendarStatusResponse)
def economic_calendar(ctx: Context) -> CalendarStatusResponse:
    """Status of scheduled economic-event data. No calendar provider is connected (D-077)."""
    status_ = calendar_status()
    with session_scope(ctx.session_factory) as session:
        stored = int(
            session.execute(select(func.count()).select_from(EconomicEventRecord)).scalar_one()
        )
    return CalendarStatusResponse(
        availability=status_.availability.value,
        provider=status_.provider,
        reason=status_.reason,
        notes=list(status_.notes),
        events_stored=stored,
    )


__all__ = ["router"]

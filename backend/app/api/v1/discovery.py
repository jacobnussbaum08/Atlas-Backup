"""Asset discovery operations API (Phase 3A): jobs, rankings, configuration, sessions.

``POST`` endpoints are operational and need ``X-Atlas-Operator-Token`` (as 2B's
historical fetch, D-027). Starting discovery calls the provider's asset directory; the
request returns at once and the job runs in the background. Requests are idempotent: while
a job of the same kind is pending or running, the existing job is returned.

Configuration changes create a new immutable version and are written to the audit log.
No endpoint here, or anywhere in Atlas, places or approves orders.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.v1.assets import Context, Page, ScoreModel, score_model
from app.api.v1.market_data import QueryTime, require_operator
from app.asset_discovery import queries
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    DiscoveryMode,
    DiscoveryOutcome,
    JobTrigger,
)
from app.asset_discovery.rules import (
    DEFAULT_RULE_SETS,
    DEFAULT_SCORING,
    RuleSetConfig,
    ScoringConfig,
    create_version,
    resolve_rule_set,
    resolve_scoring,
)
from app.asset_discovery.runtime import run_due_blocking
from app.asset_discovery.scoring import DISCLAIMER
from app.asset_discovery.sessions import CALENDARS, describe
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.data_engine.calendar import MarketSession
from app.data_engine.reference import ReferenceDataCapabilities
from app.database.models import (
    ActorType,
    Asset,
    DiscoveryJobRecord,
    DiscoveryResultRecord,
    ExchangeRecord,
    ProviderExchangeCodeRecord,
    QualificationRuleSetRecord,
    ScoringVersionRecord,
    WatchlistScoreRecord,
)
from app.database.sessions import session_scope

router = APIRouter(tags=["asset-discovery"])
Operator = [Depends(require_operator)]


# ------------------------------------------------------------------ models
class DiscoveryJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: DiscoveryJobKind
    provider: str
    status: DiscoveryJobStatus
    trigger: JobTrigger
    requested_by: str
    params: dict[str, Any]
    parent_job_id: uuid.UUID | None
    attempt: int
    max_attempts: int
    next_attempt_at: datetime
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    counters: dict[str, Any]
    summary: dict[str, Any]
    error: str | None
    recent_errors: list[dict[str, Any]]


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_asset_id: str | None
    provider_symbol: str | None
    asset_id: uuid.UUID | None
    outcome: DiscoveryOutcome
    detail: str
    data: dict[str, Any]
    created_at: datetime


class JobDetail(BaseModel):
    job: DiscoveryJob
    results_by_outcome: dict[str, int]
    results: list[DiscoveryResult]
    children: list[DiscoveryJob]


class JobRequestResponse(BaseModel):
    job: DiscoveryJob
    created: bool = Field(description="False: an equivalent job was already active")


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: DiscoveryMode = DiscoveryMode.INCREMENTAL
    # Continue with qualification, scoring and membership updates after discovery.
    pipeline: bool = True
    # QUALIFICATION skips the directory download and re-evaluates the current pool.
    start_at: Literal["DISCOVERY", "QUALIFICATION"] = "DISCOVERY"
    prepare_history: bool = Field(
        default=True, description="Retrieve bounded daily history for eligible instruments"
    )


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    max_assets: int | None = Field(default=None, ge=1, le=1000)


class RankingsResponse(BaseModel):
    job: DiscoveryJob | None
    items: list[ScoreModel]
    total: int
    notes: list[str]


class JobBrief(BaseModel):
    id: uuid.UUID
    status: DiscoveryJobStatus
    created_at: datetime
    completed_at: datetime | None
    error: str | None
    counters: dict[str, Any]


class SummaryResponse(BaseModel):
    provider: str
    catalogued: int
    by_status: dict[str, int]
    discovery_pool: int
    qualified: int
    watchlist: int
    recent_days: int
    new_listings: int
    recently_disqualified: int
    recently_deactivated: int
    last_successful_discovery: JobBrief | None
    last_jobs: dict[str, JobBrief | None]
    reference_capabilities: ReferenceDataCapabilities | None
    notes: list[str]


class ConfigVersion(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    version: int
    description: str
    config: dict[str, Any]
    content_sha256: str
    created_by: str
    created_at: datetime


class ConfigResponse(BaseModel):
    rule_set: ConfigVersion
    rule_set_versions: list[ConfigVersion]
    scoring: ConfigVersion
    scoring_versions: list[ConfigVersion]
    settings: dict[str, Any]
    notes: list[str]


class RuleSetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    config: RuleSetConfig


class ScoringBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    config: ScoringConfig


class ExchangeSession(BaseModel):
    mic: str
    name: str
    timezone: str
    calendar_code: str | None
    supported: bool
    provider_codes: list[str]
    session: MarketSession | None
    local_date: str | None
    is_trading_day: bool | None
    closure: str | None
    regular_open: datetime | None
    regular_close: datetime | None
    early_close: bool | None
    next_regular_open: datetime | None
    calendar_verified: bool | None
    halts_known: bool
    notes: list[str]


class MarketSessionsResponse(BaseModel):
    at: datetime
    exchanges: list[ExchangeSession]
    notes: list[str]


def _brief(job: DiscoveryJobRecord | None) -> JobBrief | None:
    if job is None:
        return None
    return JobBrief(
        id=job.id,
        status=job.status,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error=job.error,
        counters=job.counters,
    )


# ------------------------------------------------------------------ jobs
@router.get("/discovery/jobs", response_model=Page[DiscoveryJob])
def list_jobs(
    ctx: Context,
    kind: Annotated[list[DiscoveryJobKind] | None, Query()] = None,
    job_status: Annotated[list[DiscoveryJobStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DiscoveryJob]:
    """Discovery, refresh, qualification, scoring and membership jobs, newest first."""
    stmt = select(DiscoveryJobRecord).where(DiscoveryJobRecord.provider == ctx.provider.provider_id)
    if kind:
        stmt = stmt.where(DiscoveryJobRecord.kind.in_(kind))
    if job_status:
        stmt = stmt.where(DiscoveryJobRecord.status.in_(job_status))
    with session_scope(ctx.session_factory) as session:
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = session.scalars(
            stmt.order_by(DiscoveryJobRecord.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return Page[DiscoveryJob](
            items=[DiscoveryJob.model_validate(j) for j in rows],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/discovery/jobs/{job_id}", response_model=JobDetail)
def get_job(
    job_id: uuid.UUID,
    ctx: Context,
    outcome: Annotated[list[DiscoveryOutcome] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobDetail:
    """One job with its per-instrument results (new, changed, rejected, conflicts, missing)."""
    with session_scope(ctx.session_factory) as session:
        job = session.get(DiscoveryJobRecord, job_id)
        if job is None:
            raise NotFoundError(f"discovery job {job_id} not found")
        r = DiscoveryResultRecord
        counts = dict(
            session.execute(
                select(r.outcome, func.count()).where(r.job_id == job_id).group_by(r.outcome)
            ).all()
        )
        stmt = select(r).where(r.job_id == job_id)
        if outcome:
            stmt = stmt.where(r.outcome.in_(outcome))
        results = session.scalars(stmt.order_by(r.id).limit(limit).offset(offset)).all()
        children = session.scalars(
            select(DiscoveryJobRecord)
            .where(DiscoveryJobRecord.parent_job_id == job_id)
            .order_by(DiscoveryJobRecord.created_at)
        ).all()
        return JobDetail(
            job=DiscoveryJob.model_validate(job),
            results_by_outcome={k.value: int(v) for k, v in counts.items()},
            results=[DiscoveryResult.model_validate(x) for x in results],
            children=[DiscoveryJob.model_validate(c) for c in children],
        )


def _start(
    ctx: Any,
    background: BackgroundTasks,
    response: Response,
    kind: DiscoveryJobKind,
    params: dict[str, Any],
) -> JobRequestResponse:
    created = ctx.service.request(kind, trigger=JobTrigger.API, requested_by="api", params=params)
    if created.created:
        background.add_task(run_due_blocking, ctx.service, created.job.id)
    else:
        response.status_code = status.HTTP_200_OK
    return JobRequestResponse(job=DiscoveryJob.model_validate(created.job), created=created.created)


@router.post(
    "/discovery/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobRequestResponse,
    dependencies=Operator,
)
def run_discovery(
    body: RunBody, ctx: Context, background: BackgroundTasks, response: Response
) -> JobRequestResponse:
    """Start directory discovery (and, by default, the qualification pipeline).

    202 with a new job; 200 with the already-active job of the same kind."""
    if body.start_at == "QUALIFICATION":
        params: dict[str, Any] = {"prepare_history": body.prepare_history}
        if body.pipeline:
            params |= {"pipeline": True, "pipeline_step": 0}
        return _start(ctx, background, response, DiscoveryJobKind.QUALIFICATION, params)
    params = {"mode": body.mode.value, "pipeline": body.pipeline}
    return _start(ctx, background, response, DiscoveryJobKind.DIRECTORY_DISCOVERY, params)


@router.post(
    "/discovery/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobRequestResponse,
    dependencies=Operator,
)
def refresh_metadata(
    body: RefreshBody, ctx: Context, background: BackgroundTasks, response: Response
) -> JobRequestResponse:
    """Re-read reference data for chosen assets (or the tracked universe) one by one."""
    params: dict[str, Any] = {}
    if body.asset_ids:
        params["asset_ids"] = [str(a) for a in body.asset_ids]
    if body.max_assets is not None:
        params["max_assets"] = body.max_assets
    return _start(ctx, background, response, DiscoveryJobKind.METADATA_REFRESH, params)


# ------------------------------------------------------------------ rankings and summary
@router.get("/discovery/rankings", response_model=RankingsResponse)
def rankings(
    ctx: Context,
    job_id: uuid.UUID | None = None,
    selected_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RankingsResponse:
    """Scores of a scoring job (default: the newest successful one), best first."""
    with session_scope(ctx.session_factory) as session:
        if job_id is None:
            job_id = queries.latest_job_id(
                session, DiscoveryJobKind.WATCHLIST_SCORING, ctx.provider.provider_id
            )
        job = None if job_id is None else session.get(DiscoveryJobRecord, job_id)
        if job_id is not None and (
            job is None or job.kind is not DiscoveryJobKind.WATCHLIST_SCORING
        ):
            raise NotFoundError(f"scoring job {job_id} not found")
        items: list[ScoreModel] = []
        total = 0
        if job is not None:
            s = WatchlistScoreRecord
            stmt = (
                select(s, Asset.symbol)
                .join(Asset, Asset.id == s.asset_id)
                .where(s.job_id == job.id)
            )
            if selected_only:
                stmt = stmt.where(s.selected.is_(True))
            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            rows = session.execute(
                stmt.order_by(s.rank.asc().nulls_last(), Asset.symbol).limit(limit).offset(offset)
            ).all()
            items = [score_model(score, symbol) for score, symbol in rows]
        return RankingsResponse(
            job=None if job is None else DiscoveryJob.model_validate(job),
            items=items,
            total=total,
            notes=[
                DISCLAIMER,
                "Factor scores are percentiles within this evaluation's qualified assets (or a "
                "band score for volatility); with few qualified assets they carry little "
                "information.",
            ],
        )


@router.get("/discovery/summary", response_model=SummaryResponse)
def discovery_summary(ctx: Context) -> SummaryResponse:
    """Counts for the dashboard: catalog, layers, recent changes, last jobs."""
    provider = ctx.provider.provider_id
    with session_scope(ctx.session_factory) as session:
        data = queries.summary(session, provider, ctx.clock.now())
        layers = data["layers"]
        return SummaryResponse(
            provider=provider,
            catalogued=data["catalogued"],
            by_status=data["by_status"],
            discovery_pool=layers["DISCOVERY_POOL"],
            qualified=layers["QUALIFIED_UNIVERSE"],
            watchlist=layers["ACTIVE_WATCHLIST"],
            recent_days=data["recent_days"],
            new_listings=data["new_listings"],
            recently_disqualified=data["recently_disqualified"],
            recently_deactivated=data["recently_deactivated"],
            last_successful_discovery=_brief(data["last_successful_discovery"]),
            last_jobs={k: _brief(v) for k, v in data["last_jobs"].items()},
            reference_capabilities=ctx.provider.reference_capabilities(),
            notes=[
                "The discovery pool contains what the configured provider's directory lists; "
                "it is not every publicly traded security.",
                "New listings are instruments that appeared after the initial catalog, dated "
                "by when Atlas noticed them.",
            ],
        )


# ------------------------------------------------------------------ configuration
def _versions(session: Any, model: Any, code: str) -> list[ConfigVersion]:
    rows = session.scalars(select(model).where(model.code == code).order_by(model.version.desc()))
    return [ConfigVersion.model_validate(r) for r in rows]


@router.get("/discovery/config", response_model=ConfigResponse)
def discovery_config(ctx: Context) -> ConfigResponse:
    """Rule set and scoring version in use (newest version of each configured code)."""
    settings = ctx.settings
    code = settings.rule_set_code(ctx.provider.feed)
    with session_scope(ctx.session_factory) as session:
        rule_set, _ = resolve_rule_set(session, code)
        scoring, _ = resolve_scoring(session, settings.scoring_version)
        return ConfigResponse(
            rule_set=ConfigVersion.model_validate(rule_set),
            rule_set_versions=_versions(session, QualificationRuleSetRecord, code),
            scoring=ConfigVersion.model_validate(scoring),
            scoring_versions=_versions(session, ScoringVersionRecord, settings.scoring_version),
            settings=settings.model_dump(mode="json"),
            notes=[
                "Thresholds and weights are initial defaults, not empirically validated.",
                "A change creates a new immutable version (audited); evaluations keep the "
                "version they used. Operational settings come from ASSET_DISCOVERY_* "
                "variables and are read-only here.",
                f"Default rule sets: {', '.join(DEFAULT_RULE_SETS)}; default scoring: "
                f"{', '.join(DEFAULT_SCORING)}.",
            ],
        )


def _create_config(
    ctx: Any, model: Any, code: str, body: RuleSetBody | ScoringBody
) -> ConfigVersion:
    with session_scope(ctx.session_factory) as session:
        record = create_version(
            session,
            model,
            code=code,
            config=body.config,
            description=body.description,
            created_by="operator",
            actor_type=ActorType.USER,
            reason=body.reason,
        )
        return ConfigVersion.model_validate(record)


@router.post(
    "/discovery/config/rule-set",
    status_code=status.HTTP_201_CREATED,
    response_model=ConfigVersion,
    dependencies=Operator,
)
def create_rule_set_version(body: RuleSetBody, ctx: Context) -> ConfigVersion:
    """New version of the configured rule set. Its feed must equal the configured feed."""
    if body.config.feed != ctx.provider.feed:
        raise InvalidRequestError(
            f"rule set feed '{body.config.feed}' differs from the configured feed "
            f"'{ctx.provider.feed}'"
        )
    return _create_config(
        ctx, QualificationRuleSetRecord, ctx.settings.rule_set_code(ctx.provider.feed), body
    )


@router.post(
    "/discovery/config/scoring",
    status_code=status.HTTP_201_CREATED,
    response_model=ConfigVersion,
    dependencies=Operator,
)
def create_scoring_version(body: ScoringBody, ctx: Context) -> ConfigVersion:
    total = sum(body.config.weights.values(), Decimal(0))
    if total <= 0:
        raise InvalidRequestError("scoring weights must sum to more than zero")
    return _create_config(ctx, ScoringVersionRecord, ctx.settings.scoring_version, body)


# ------------------------------------------------------------------ sessions
@router.get("/market-sessions", response_model=MarketSessionsResponse)
def market_sessions(ctx: Context, at: QueryTime | None = None) -> MarketSessionsResponse:
    """Session state per listing venue at ``at`` (default now), in the venue's time zone."""
    when = at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        venues = session.scalars(select(ExchangeRecord).order_by(ExchangeRecord.mic)).all()
        codes: dict[str, list[str]] = {}
        for row in session.scalars(select(ProviderExchangeCodeRecord)):
            if row.mic is not None:
                codes.setdefault(row.mic, []).append(f"{row.provider}:{row.provider_exchange}")
        result = []
        for venue in venues:
            calendar = CALENDARS.get(venue.calendar_code) if venue.calendar_code else None
            notes = [venue.notes] if venue.notes else []
            if calendar is None:
                result.append(
                    ExchangeSession(
                        mic=venue.mic,
                        name=venue.name,
                        timezone=venue.timezone,
                        calendar_code=venue.calendar_code,
                        supported=False,
                        provider_codes=codes.get(venue.mic, []),
                        session=None,
                        local_date=None,
                        is_trading_day=None,
                        closure=None,
                        regular_open=None,
                        regular_close=None,
                        early_close=None,
                        next_regular_open=None,
                        calendar_verified=None,
                        halts_known=False,
                        notes=[
                            *notes,
                            "No verified session calendar: Atlas cannot tell a closed "
                            "market from missing data here.",
                        ],
                    )
                )
                continue
            state = describe(calendar, when)
            result.append(
                ExchangeSession(
                    mic=venue.mic,
                    name=venue.name,
                    timezone=venue.timezone,
                    calendar_code=calendar.code,
                    supported=True,
                    provider_codes=codes.get(venue.mic, []),
                    session=state.session,
                    local_date=state.local_date.isoformat(),
                    is_trading_day=state.is_trading_day,
                    closure=state.closure,
                    regular_open=None if state.today is None else state.today.regular_open,
                    regular_close=None if state.today is None else state.today.regular_close,
                    early_close=None if state.today is None else state.today.early_close,
                    next_regular_open=state.next_regular_open,
                    calendar_verified=state.verified,
                    halts_known=state.halts_known,
                    notes=[*notes, *state.notes],
                )
            )
        return MarketSessionsResponse(
            at=when,
            exchanges=result,
            notes=[
                "All times are UTC; local dates are the venue's.",
                "Extended hours use the generic US 04:00-20:00 ET window; venue-specific early "
                "and late sessions and overnight trading are not modelled.",
                "Holiday rules are verified against NYSE's published calendar for 2025-2027; "
                "other years are rule-derived and marked unverified.",
            ],
        )


__all__ = ["router"]

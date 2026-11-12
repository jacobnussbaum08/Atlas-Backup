"""Market-data API (2B: historical retrieval and inspection; 2C live endpoints are in
``market_data_stream``).

Read endpoints are open on the local API. Starting a retrieval is an operational
action: it requires ``X-Atlas-Operator-Token`` when ``ATLAS_OPERATOR_TOKEN`` is set,
and is refused outside development/test when it is not. No endpoint exposes
credentials, and there are no trading or order endpoints.
"""

from __future__ import annotations

import hmac
import threading
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clocks import ensure_utc
from app.core.config import Environment, Settings
from app.core.exceptions import (
    DependencyUnavailableError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.security import operators
from app.core.security.operators import OperatorIdentity
from app.data_engine.availability import POLICY_VERSION
from app.data_engine.capabilities import FeedCoverage, ProviderCapabilities
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    IngestionJobStatus,
    PriceAdjustment,
    Severity,
)
from app.data_engine.historical import HistoricalFetchRequest, job_snapshot
from app.data_engine.providers.base import ProviderConfigurationReport
from app.data_engine.runtime import MarketDataContext, run_job_blocking
from app.data_engine.schemas import CredentialStatus, DataIngestionJob, MarketAsset
from app.data_engine.timeframes import Timeframe, TimeframeField
from app.database.models import Asset, DataQualityEventRecord, MarketBarRecord
from app.database.repositories import (
    AssetMappingRepository,
    IngestionJobRepository,
    MarketBarRepository,
    MarketDataEventRepository,
    SeriesKey,
)
from app.database.sessions import session_scope

router = APIRouter(prefix="/market-data", tags=["market-data"])

QueryTime = Annotated[datetime, AfterValidator(ensure_utc)]
_context_lock = threading.Lock()


# ------------------------------------------------------------------ dependencies
def market_data(request: Request) -> MarketDataContext:
    state = request.app.state
    context: MarketDataContext | None = getattr(state, "market_data", None)
    if context is None:
        factory = getattr(state, "market_data_factory", None)
        if factory is None:
            raise DependencyUnavailableError("market data is not configured on this server")
        with _context_lock:
            context = getattr(state, "market_data", None)
            if context is None:
                context = factory()
                state.market_data = context
    return context


def _presented(token: str | None, authorization: str | None) -> str | None:
    if token:
        return token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def require_operator(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Atlas-Operator-Token")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> OperatorIdentity:
    """Who is acting. A named operator credential (Phase 10B) identifies ``operator:<name>``;
    the shared ``ATLAS_OPERATOR_TOKEN`` identifies ``operator:api``; with neither configured,
    development and test environments allow the unauthenticated ``operator:api``."""
    settings: Settings = request.app.state.settings
    expected = settings.operator_token
    presented = _presented(token, authorization)
    if presented is not None:
        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None and presented.startswith(operators.TOKEN_PREFIX):
            with session_scope(factory) as session:
                identity = operators.authenticate(session, presented, request.app.state.clock.now())
            if identity is not None:
                return identity
        if expected is not None and hmac.compare_digest(
            presented.encode("utf-8"), expected.get_secret_value().encode("utf-8")
        ):
            return OperatorIdentity("operator:api", "SHARED", can_control_trading=True)
        raise UnauthorizedError("missing or invalid X-Atlas-Operator-Token")
    if expected is None:
        if settings.environment in (Environment.DEVELOPMENT, Environment.TEST):
            return OperatorIdentity("operator:api", "DEVELOPMENT", can_control_trading=True)
        raise ForbiddenError(
            "operational endpoints need a named operator credential or ATLAS_OPERATOR_TOKEN"
        )
    raise UnauthorizedError("missing or invalid X-Atlas-Operator-Token")


def require_trading_operator(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator)],
) -> OperatorIdentity:
    """The trading controls (pause, disable, emergency stop, kill switch). In the paper
    environment only a named operator credential issued with trading control may use them;
    the shared token may not (D-218)."""
    settings: Settings = request.app.state.settings
    if settings.environment is Environment.PAPER and not identity.named:
        raise ForbiddenError(
            "trading controls need a named operator credential in the paper environment"
        )
    if identity.named and not identity.can_control_trading:
        raise ForbiddenError(f"{identity.actor} may not use the trading controls")
    return identity


Context = Annotated[MarketDataContext, Depends(market_data)]


# ------------------------------------------------------------------ models
class HistoricalFetchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=list, description="Atlas symbols, e.g. AAPL")
    asset_ids: list[uuid.UUID] = Field(default_factory=list)
    timeframe: TimeframeField = Field(description="1Min, 5Min, 15Min, 1Hour or 1Day")
    start: Annotated[datetime, AfterValidator(ensure_utc)]
    end: Annotated[datetime, AfterValidator(ensure_utc)]
    feed: str | None = Field(default=None, description="Must equal the configured feed")


class ProviderInfo(BaseModel):
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    adjustment: PriceAdjustment
    credentials: CredentialStatus
    capabilities: ProviderCapabilities
    configuration: ProviderConfigurationReport


class Candle(BaseModel):
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    vwap: Decimal | None
    trade_count: int | None
    is_complete: bool
    quality_status: DataQualityStatus
    revision: int
    provider_symbol: str
    source: DataSource
    observation_kind: BarObservationKind
    received_at: datetime
    available_at: datetime
    original_available_at: datetime | None
    processed_at: datetime
    ingested_at: datetime
    ingestion_job_id: uuid.UUID | None
    raw_event_id: int | None


class CandlesResponse(BaseModel):
    asset: MarketAsset
    timeframe: str
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    adjustment: PriceAdjustment
    quality: list[DataQualityStatus]
    known_at: datetime | None
    candles: list[Candle]
    next_after: datetime | None = Field(description="Pass as `after` for the next page")
    notes: list[str]


class CoverageEntry(BaseModel):
    timeframe: str
    provider: str
    feed: str
    adjustment: str
    first_event_at: datetime
    last_event_at: datetime
    bars: int
    quarantined: int
    incomplete: int


class HistoryResponse(BaseModel):
    asset: MarketAsset
    coverage: list[CoverageEntry]
    notes: list[str]


class QualityEvent(BaseModel):
    id: int
    event_type: DataQualityEventType
    severity: Severity
    provider: str
    feed: str | None
    asset_id: uuid.UUID | None
    provider_symbol: str | None
    timeframe: str | None
    interval_start: datetime | None
    interval_end: datetime | None
    detected_at: datetime
    resulting_status: DataQualityStatus | None
    message: str
    details: dict[str, object]
    ingestion_job_id: uuid.UUID | None
    raw_event_id: int | None


def _quality_event(record: DataQualityEventRecord) -> QualityEvent:
    return QualityEvent.model_validate(record, from_attributes=True)


def _market_asset(asset: Asset, provider: str, provider_symbol: str | None) -> MarketAsset:
    return MarketAsset(
        asset_id=asset.id,
        symbol=asset.symbol,
        name=asset.name,
        asset_class=asset.asset_class,
        exchange_code=asset.exchange_code,
        quote_currency=asset.quote_currency,
        status=asset.status,
        provider=provider,
        provider_symbol=provider_symbol,
    )


def _find_asset(session: Session, symbol: str | None, asset_id: uuid.UUID | None) -> Asset:
    if (symbol is None) == (asset_id is None):
        raise NotFoundError("give exactly one of 'symbol' or 'asset_id'")
    if asset_id is not None:
        asset = session.get(Asset, asset_id)
    else:
        found = session.scalars(select(Asset).where(Asset.symbol == symbol)).all()
        if len(found) > 1:
            raise NotFoundError(f"'{symbol}' is ambiguous; query by asset_id")
        asset = found[0] if found else None
    if asset is None:
        raise NotFoundError("asset not found")
    return asset


# ------------------------------------------------------------------ endpoints
@router.get("/providers", response_model=list[ProviderInfo])
def providers(ctx: Context) -> list[ProviderInfo]:
    """Configured provider, its capabilities and an offline configuration report."""
    provider = ctx.provider
    return [
        ProviderInfo(
            provider=provider.provider_id,
            feed=provider.feed,
            feed_coverage=provider.feed_access().coverage,
            adjustment=ctx.settings.historical_data_adjustment,
            credentials=provider.credential_status(),
            capabilities=provider.capabilities(),
            configuration=provider.validate_configuration(ctx.settings),
        )
    ]


@router.post(
    "/historical-fetch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataIngestionJob,
    dependencies=[Depends(require_operator)],
)
def historical_fetch(
    body: HistoricalFetchBody, ctx: Context, background: BackgroundTasks
) -> DataIngestionJob:
    """Create a historical bar ingestion job and run it in the background."""
    request = HistoricalFetchRequest(
        symbols=tuple(s.strip().upper() for s in body.symbols),
        asset_ids=tuple(body.asset_ids),
        timeframe=body.timeframe,
        start=body.start,
        end=body.end,
        feed=body.feed,
    )
    job = ctx.service.create_job(request, requested_by="api")
    background.add_task(run_job_blocking, ctx.service, job.job_id)
    return job


@router.get("/ingestion-jobs", response_model=list[DataIngestionJob])
def ingestion_jobs(
    ctx: Context,
    job_status: Annotated[list[IngestionJobStatus] | None, Query(alias="status")] = None,
    symbol: Annotated[str | None, Query(description="Jobs that include this asset")] = None,
    timeframe: Annotated[str | None, Query(description="e.g. 1Min; bar jobs only")] = None,
    created_from: QueryTime | None = None,
    created_to: QueryTime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DataIngestionJob]:
    """Jobs, newest first. Filter `status=FAILED&status=PARTIALLY_COMPLETED` for failures;
    `created_from`/`created_to` bound the creation time (half-open)."""
    tf = None
    if timeframe is not None:
        try:
            tf = str(Timeframe.parse(timeframe))
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from None
    if created_from is not None and created_to is not None and created_to <= created_from:
        raise InvalidRequestError("created_to must be after created_from")
    with session_scope(ctx.session_factory) as session:
        asset_id = _find_asset(session, symbol.upper(), None).id if symbol else None
        repo = IngestionJobRepository(session)
        jobs = repo.list(
            statuses=job_status,
            limit=limit,
            offset=offset,
            asset_id=asset_id,
            timeframe=tf,
            created_from=created_from,
            created_to=created_to,
        )
        return [job_snapshot(job, repo.symbols(job.id)) for job in jobs]


@router.get("/ingestion-jobs/{job_id}", response_model=DataIngestionJob)
def ingestion_job(job_id: uuid.UUID, ctx: Context) -> DataIngestionJob:
    with session_scope(ctx.session_factory) as session:
        repo = IngestionJobRepository(session)
        job = repo.get(job_id)
        if job is None:
            raise NotFoundError(f"ingestion job {job_id} not found")
        return job_snapshot(job, repo.symbols(job_id))


@router.get("/quality-events", response_model=list[QualityEvent])
def quality_events(
    ctx: Context,
    job_id: uuid.UUID | None = None,
    asset_id: uuid.UUID | None = None,
    event_type: Annotated[list[DataQualityEventType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[QualityEvent]:
    with session_scope(ctx.session_factory) as session:
        events = MarketDataEventRepository(session).quality_events(
            job_id=job_id,
            asset_id=asset_id,
            event_types=[e.value for e in event_type] if event_type else None,
            limit=limit,
            offset=offset,
        )
        return [_quality_event(e) for e in events]


@router.get("/assets", response_model=list[MarketAsset])
def assets(ctx: Context) -> list[MarketAsset]:
    """Assets with an active mapping at the configured provider."""
    provider = ctx.provider.provider_id
    with session_scope(ctx.session_factory) as session:
        return [
            _market_asset(asset, provider, mapping.provider_symbol)
            for asset, mapping in AssetMappingRepository(session).current_assets(provider)
        ]


@router.get("/assets/{asset_id}/history", response_model=HistoryResponse)
def asset_history(asset_id: uuid.UUID, ctx: Context) -> HistoryResponse:
    """Stored historical ranges per timeframe/feed/adjustment (current revisions)."""
    provider = ctx.provider.provider_id
    with session_scope(ctx.session_factory) as session:
        asset = _find_asset(session, None, asset_id)
        mapping = AssetMappingRepository(session).current_for_asset(asset.id, provider)
        coverage = MarketBarRepository(session).coverage(asset.id)
        return HistoryResponse(
            asset=_market_asset(asset, provider, mapping.provider_symbol if mapping else None),
            coverage=[CoverageEntry(**vars(row)) for row in coverage],
            notes=[
                "Ranges are first/last stored bar; gaps inside a range are not computed yet "
                "(that needs exchange calendars, Phase 3).",
                "Providers emit no bar for intervals without trades, so a gap is not "
                "necessarily missing data.",
            ],
        )


@router.get("/candles", response_model=CandlesResponse)
def candles(
    ctx: Context,
    timeframe: Annotated[str, Query(description="1Min, 5Min, 15Min, 1Hour or 1Day")],
    start: QueryTime,
    end: QueryTime,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    feed: str | None = None,
    adjustment: PriceAdjustment | None = None,
    quality: Annotated[list[DataQualityStatus] | None, Query()] = None,
    known_at: QueryTime | None = None,
    after: QueryTime | None = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
) -> CandlesResponse:
    """Current revision of each bar in [start, end] for one series.

    Defaults: the configured feed and adjustment, and only VALID bars. Series with
    different feeds or adjustments are never merged. ``known_at`` returns what Atlas
    had stored at that time (later revisions excluded).
    """
    try:
        tf = str(Timeframe.parse(timeframe))
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from None
    if end < start:
        raise InvalidRequestError("end must not be before start")
    provider = ctx.provider
    series_feed = feed or provider.feed
    series_adjustment = adjustment or ctx.settings.historical_data_adjustment
    statuses = quality or [DataQualityStatus.VALID]
    with session_scope(ctx.session_factory) as session:
        asset = _find_asset(session, symbol.upper() if symbol else None, asset_id)
        mapping = AssetMappingRepository(session).current_for_asset(asset.id, provider.provider_id)
        key = SeriesKey(asset.id, tf, provider.provider_id, series_feed, series_adjustment)
        rows: list[MarketBarRecord] = list(
            MarketBarRepository(session).current_bars(
                key,
                start=start,
                end=end,
                statuses=statuses,
                known_at=known_at,
                after=after,
                limit=limit,
            )
        )
        coverage = provider.feed_access(series_feed).coverage
        notes = [
            f"Backfilled bars (HISTORICAL_REST): original availability unknown "
            f"(original_available_at is null). Backtests must apply availability policy "
            f"'{POLICY_VERSION}'.",
            "Live bars (LIVE_STREAM): LIVE_INITIAL is not final (late trades produce "
            "LIVE_UPDATED revisions); LIVE_RUNNING bars are still forming. Use known_at for "
            "the version Atlas had at a past time.",
            f"Prices are {series_adjustment.value}: not adjusted for corporate actions."
            if series_adjustment is PriceAdjustment.RAW
            else f"Prices use {series_adjustment.value} adjustment.",
        ]
        if coverage is FeedCoverage.SINGLE_VENUE:
            notes.append(
                f"Feed '{series_feed}' is a single venue: volumes and prices do not "
                "represent consolidated US market activity."
            )
        return CandlesResponse(
            asset=_market_asset(
                asset, provider.provider_id, mapping.provider_symbol if mapping else None
            ),
            timeframe=tf,
            provider=provider.provider_id,
            feed=series_feed,
            feed_coverage=coverage,
            adjustment=series_adjustment,
            quality=statuses,
            known_at=known_at,
            candles=[Candle.model_validate(row, from_attributes=True) for row in rows],
            next_after=rows[-1].event_time if len(rows) == limit else None,
            notes=notes,
        )

"""Completeness, recovery and pipeline health (Deliverable 2D).

Read-only except ``POST /market-data/recovery-requests``, which only *records* a request
(gaps for the parts of a range without completeness evidence); the recovery worker
retrieves the data. It requires the operator token like other operational actions.

``/pipeline-health`` separates provider connectivity from the health of the whole data
pipeline: a connected, authenticated stream can still be FAILING because writes fail.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.v1.market_data import Context, QueryTime, _find_asset, require_operator
from app.api.v1.market_data_stream import _view
from app.core.exceptions import InvalidRequestError
from app.data_engine.completeness import Interval, UnsupportedTimeframeError, total
from app.data_engine.enums import (
    DataType,
    GapCause,
    GapStatus,
    RecoveryJobKind,
    RecoveryJobStatus,
    Severity,
    StreamAuthState,
    StreamGapKind,
    StreamHealth,
    StreamSubscriptionState,
)
from app.data_engine.errors import MarketDataError
from app.data_engine.recovery import RecoveryService
from app.data_engine.stream_health import DEGRADED_WINDOW, derive_health
from app.data_engine.timeframes import Timeframe
from app.database.models import (
    DataQualityEventRecord,
    MarketDataGapRecord,
    RecoveryJobRecord,
    StreamGapRecord,
    StreamSessionRecord,
)
from app.database.repositories import (
    CompletenessRepository,
    IngestionJobRepository,
    RecoveryJobRepository,
    RecoveryStatusRepository,
    StreamRepository,
)
from app.database.sessions import session_scope

router = APIRouter(prefix="/market-data", tags=["market-data"])

RECOVERY_HEARTBEAT_GRACE = timedelta(seconds=60)


class DimensionState(StrEnum):
    OK = "OK"
    WARNING = "WARNING"  # working, with a condition that needs attention
    FAILING = "FAILING"  # the stage is not doing its job
    INACTIVE = "INACTIVE"  # not expected to be active (disabled, or market closed)
    UNKNOWN = "UNKNOWN"  # no information yet


class PipelineHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    INACTIVE = "INACTIVE"


# ------------------------------------------------------------------ models
class IntervalOut(BaseModel):
    start: datetime
    end: datetime


class CompletenessResponse(BaseModel):
    symbol: str
    provider: str
    feed: str
    data_type: DataType
    timeframe: str | None
    start: datetime
    end: datetime
    status: str = Field(
        description="COMPLETE (all expected data verified), INCOMPLETE (some missing or "
        "unverified), MISSING (nothing verified), NOT_EXPECTED (market closed throughout)"
    )
    expected_seconds: float
    covered_seconds: float
    expected: list[IntervalOut]
    covered: list[IntervalOut]
    missing: list[IntervalOut]
    unverified: list[IntervalOut] = Field(
        description="Trades collected live but not yet reconciled with provider history"
    )
    known_gaps: list[GapOut]
    notes: list[str]


class GapOut(BaseModel):
    id: int
    asset_id: uuid.UUID
    provider_symbol: str
    provider: str
    feed: str
    data_type: DataType
    timeframe: str | None
    gap_start: datetime
    gap_end: datetime
    cause: GapCause
    status: GapStatus
    stream_gap_id: int | None
    parent_gap_id: int | None
    detected_at: datetime
    resolved_at: datetime | None
    attempts: int
    records_retrieved: int
    records_recovered: int
    records_duplicate: int
    records_revised: int
    records_rejected: int
    resolution: str | None
    details: dict[str, Any]


class RecoveryJobOut(BaseModel):
    id: uuid.UUID
    kind: RecoveryJobKind
    gap_id: int | None
    asset_id: uuid.UUID
    provider_symbol: str
    provider: str
    feed: str
    data_type: DataType
    timeframe: str | None
    missing_start: datetime
    missing_end: datetime
    requested_start: datetime
    requested_end: datetime
    status: RecoveryJobStatus
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    ingestion_job_ids: list[uuid.UUID]
    records_retrieved: int
    records_recovered: int
    records_duplicate: int
    records_revised: int
    records_rejected: int
    remaining: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class RecoveryRequest(BaseModel):
    symbol: str
    data_type: DataType = DataType.BAR
    timeframe: str | None = None
    start: datetime
    end: datetime


class RecoveryRequestResponse(BaseModel):
    gaps: list[GapOut]
    note: str


class Dimension(BaseModel):
    name: str
    state: DimensionState
    detail: str
    essential: bool
    last_success_at: datetime | None = Field(
        default=None, description="Last time this stage demonstrably did its job"
    )
    updated_at: datetime | None = Field(
        default=None, description="When the process behind this stage last reported"
    )


class PipelineHealthResponse(BaseModel):
    provider: str
    feed: str
    pipeline_health: PipelineHealth
    stream_health: StreamHealth
    dimensions: list[Dimension]
    last_message_at: datetime | None
    last_persisted_at: datetime | None
    last_successful_backfill_at: datetime | None
    outstanding_gaps: dict[str, int]
    pending_recovery_jobs: int
    failed_recovery_jobs: int
    recent_provider_errors: list[dict[str, Any]]
    checked_at: datetime


# ------------------------------------------------------------------ helpers
def _intervals(items: list[Interval]) -> list[IntervalOut]:
    return [IntervalOut(start=s, end=e) for s, e in items]


def _gap(record: MarketDataGapRecord) -> GapOut:
    return GapOut.model_validate(record, from_attributes=True)


def _job(record: RecoveryJobRecord) -> RecoveryJobOut:
    return RecoveryJobOut.model_validate(record, from_attributes=True)


def _service(ctx: Any, request: Request) -> RecoveryService:
    return RecoveryService(
        ctx.session_factory, ctx.provider, ctx.settings, request.app.state.clock, ctx.service
    )


def _timeframe(data_type: DataType, timeframe: str | None) -> Timeframe | None:
    if data_type is DataType.BAR:
        if timeframe is None:
            raise InvalidRequestError("bars need a timeframe")
        try:
            return Timeframe.parse(timeframe)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from None
    if timeframe is not None:
        raise InvalidRequestError("trades and quotes take no timeframe")
    return None


# ------------------------------------------------------------------ completeness
@router.get("/completeness", response_model=CompletenessResponse)
def completeness(
    ctx: Context,
    request: Request,
    symbol: str,
    start: QueryTime,
    end: QueryTime,
    data_type: DataType = DataType.BAR,
    timeframe: str | None = None,
) -> CompletenessResponse:
    """Whether expected data in [start, end) is verified complete, and what is missing.

    Expected data follows the US equity calendar and the feed's hours; minutes without a
    bar inside verified time had no eligible activity and are not missing.
    """
    if end <= start:
        raise InvalidRequestError("end must be after start")
    if end - start > timedelta(days=31):
        raise InvalidRequestError("ranges are limited to 31 days")
    tf = _timeframe(data_type, timeframe)
    service = _service(ctx, request)
    try:
        tracked = service._tracked_symbol(symbol.upper(), data_type, tf)
    except UnsupportedTimeframeError as exc:
        raise InvalidRequestError(str(exc)) from None
    except MarketDataError as exc:
        raise InvalidRequestError(exc.message) from None
    now = request.app.state.clock.now()
    state = service.evaluate(tracked, (start, end), now)
    with session_scope(ctx.session_factory) as session:
        known = CompletenessRepository(session).unresolved_gaps(tracked.series, (start, end))
        gaps = [_gap(g) for g in known]
    expected_seconds = total(state["expected"]).total_seconds()
    covered_seconds = total(state["covered"]).total_seconds()
    if not state["expected"]:
        verdict = "NOT_EXPECTED"
    elif not state["missing"] and not state["unverified"]:
        verdict = "COMPLETE"
    elif not state["covered"]:
        verdict = "MISSING"
    else:
        verdict = "INCOMPLETE"
    notes = [
        "Expected data: NYSE trading days (holidays and early closes excluded) within the "
        f"'{ctx.provider.feed}' feed's hours, restricted to intervals that have settled.",
        "Verified = a complete retrieval of the provider's history, or (bars only) an "
        "uninterrupted live stream. Missing minutes inside verified time had no activity.",
    ]
    if end > now:
        notes.append("The range extends into the future or the unsettled recent past.")
    return CompletenessResponse(
        symbol=symbol.upper(),
        provider=ctx.provider.provider_id,
        feed=ctx.provider.feed,
        data_type=data_type,
        timeframe=None if tf is None else str(tf),
        start=start,
        end=end,
        status=verdict,
        expected_seconds=expected_seconds,
        covered_seconds=covered_seconds,
        expected=_intervals(state["expected"]),
        covered=_intervals(state["covered"]),
        missing=_intervals(state["missing"]),
        unverified=_intervals(state["unverified"]),
        known_gaps=gaps,
        notes=notes,
    )


# ------------------------------------------------------------------ gaps and jobs
@router.get("/gaps", response_model=list[GapOut])
def gaps(
    ctx: Context,
    status_filter: Annotated[list[GapStatus] | None, Query(alias="status")] = None,
    symbol: str | None = None,
    data_type: DataType | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: int | None = None,
) -> list[GapOut]:
    """Missing intervals per series, newest first; page with ``before_id``."""
    with session_scope(ctx.session_factory) as session:
        asset_id = None if symbol is None else _find_asset(session, symbol, None).id
        records = CompletenessRepository(session).gaps(
            provider=ctx.provider.provider_id,
            feed=ctx.provider.feed,
            asset_id=asset_id,
            data_type=data_type,
            statuses=status_filter or (),
            limit=limit,
            after_id=before_id,
        )
        return [_gap(g) for g in records]


@router.get("/recovery-jobs", response_model=list[RecoveryJobOut])
def recovery_jobs(
    ctx: Context,
    status_filter: Annotated[list[RecoveryJobStatus] | None, Query(alias="status")] = None,
    gap_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[RecoveryJobOut]:
    with session_scope(ctx.session_factory) as session:
        records = RecoveryJobRepository(session).jobs(
            provider=ctx.provider.provider_id,
            feed=ctx.provider.feed,
            statuses=status_filter or (),
            gap_id=gap_id,
            limit=limit,
        )
        return [_job(j) for j in records]


@router.post(
    "/recovery-requests",
    response_model=RecoveryRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_operator)],
)
def request_recovery(
    ctx: Context, request: Request, body: RecoveryRequest
) -> RecoveryRequestResponse:
    """Record the parts of a range that lack completeness evidence as gaps; the recovery
    worker retrieves them. Parts already verified or already known are not repeated."""
    if body.end <= body.start:
        raise InvalidRequestError("end must be after start")
    if body.end - body.start > timedelta(days=31):
        raise InvalidRequestError("ranges are limited to 31 days")
    tf = _timeframe(body.data_type, body.timeframe)
    service = _service(ctx, request)
    try:
        created = service.request_recovery(
            body.symbol.upper(), body.data_type, tf, body.start, body.end, "api"
        )
    except UnsupportedTimeframeError as exc:
        raise InvalidRequestError(str(exc)) from None
    except MarketDataError as exc:
        raise InvalidRequestError(exc.message) from None
    note = (
        f"{len(created)} interval(s) recorded for recovery"
        if created
        else "nothing to recover: the range is verified or already being recovered"
    )
    if body.data_type not in ctx.settings.market_data_recovery_data_types:
        note += (
            f"; {body.data_type.value} recovery is not enabled (MARKET_DATA_RECOVERY_DATA_TYPES)"
        )
    return RecoveryRequestResponse(gaps=[_gap(g) for g in created], note=note)


# ------------------------------------------------------------------ pipeline health
@router.get("/pipeline-health", response_model=PipelineHealthResponse)
def pipeline_health(ctx: Context, request: Request) -> PipelineHealthResponse:
    """Health per pipeline stage. The pipeline is HEALTHY only if every essential stage
    is OK; connectivity alone never makes it healthy."""
    provider, feed = ctx.provider.provider_id, ctx.provider.feed
    now = request.app.state.clock.now()
    settings = ctx.settings
    with session_scope(ctx.session_factory) as session:
        row = StreamRepository(session).get_status(provider, feed)
        view = _view(row) if row is not None else None
        enabled = row.enabled if row is not None else settings.market_data_stream_enabled
        report = derive_health(
            view,
            enabled=enabled,
            now=now,
            stale_after=timedelta(seconds=settings.market_data_stream_stale_after),
        )
        repo = CompletenessRepository(session)
        counts = repo.gap_counts(provider, feed)
        outstanding = {
            data_type: sum(n for s, n in by_status.items() if s in ("OPEN", "RECOVERING"))
            for data_type, by_status in counts.items()
        }
        unavailable = sum(by_status.get("UNAVAILABLE", 0) for by_status in counts.values())
        job_counts = RecoveryJobRepository(session).job_counts(provider, feed)
        recent_failed = session.scalar(
            select(func.count()).where(
                RecoveryJobRecord.provider == provider,
                RecoveryJobRecord.feed == feed,
                RecoveryJobRecord.status == RecoveryJobStatus.FAILED,
                RecoveryJobRecord.completed_at >= now - timedelta(hours=24),
            )
        )
        recovery = RecoveryStatusRepository(session).get(provider, feed)
        last_backfill = RecoveryJobRepository(session).last_completed_at(provider, feed)
        validation_errors = dict(
            session.execute(
                select(DataQualityEventRecord.severity, func.count())
                .where(
                    DataQualityEventRecord.provider == provider,
                    DataQualityEventRecord.feed == feed,
                    DataQualityEventRecord.detected_at >= now - DEGRADED_WINDOW,
                    DataQualityEventRecord.severity.in_([Severity.ERROR, Severity.CRITICAL]),
                )
                .group_by(DataQualityEventRecord.severity)
            ).all()
        )
        recent_errors = list(row.recent_errors or [])[-10:] if row is not None else []

    stream_active = report.health not in (StreamHealth.DISABLED,)
    dims: list[Dimension] = []

    heartbeat = row.heartbeat_at if row is not None else None
    stage_success = {
        "provider_connection": row.last_message_at if row is not None else None,
        "authentication": row.last_authenticated_at if row is not None else None,
        "subscription": row.last_subscribed_at if row is not None else None,
        "message_reception": row.last_message_at if row is not None else None,
        "message_processing": row.last_persisted_at if row is not None else None,
        "database_persistence": row.last_persisted_at if row is not None else None,
        "historical_completeness": last_backfill,
        "backfill_activity": last_backfill,
    }
    stage_updated = {
        "historical_completeness": recovery.heartbeat_at if recovery is not None else None,
        "backfill_activity": recovery.heartbeat_at if recovery is not None else None,
    }

    def add(name: str, state: DimensionState, detail: str, essential: bool = True) -> None:
        dims.append(
            Dimension(
                name=name,
                state=state,
                detail=detail,
                essential=essential,
                last_success_at=stage_success.get(name),
                updated_at=stage_updated.get(name, heartbeat),
            )
        )

    inactive = DimensionState.INACTIVE
    if not stream_active:
        for name in (
            "provider_connection",
            "authentication",
            "subscription",
            "message_reception",
            "message_processing",
            "data_validation",
            "database_persistence",
        ):
            add(name, inactive, "live streaming is disabled")
    else:
        metrics = row.metrics if row is not None else {}
        connected = report.health not in (
            StreamHealth.NOT_RUNNING,
            StreamHealth.RECONNECTING,
            StreamHealth.FAILED,
            StreamHealth.STARTING,
        )
        add(
            "provider_connection",
            DimensionState.OK if connected else DimensionState.FAILING,
            "; ".join(report.reasons) if not connected else f"connected ({report.health.value})",
        )
        # The recorded auth/subscription states describe the last connection; without a
        # live connection they are history, not the current state (2E).
        auth = row.auth_state if row is not None else None
        if auth == StreamAuthState.REJECTED:
            auth_state, auth_detail = DimensionState.FAILING, f"auth state {auth}"
        elif not connected:
            auth_state = DimensionState.UNKNOWN
            auth_detail = f"not connected (last known auth state {auth})"
        elif auth == StreamAuthState.AUTHENTICATED:
            auth_state, auth_detail = DimensionState.OK, f"auth state {auth}"
        else:
            auth_state, auth_detail = DimensionState.UNKNOWN, f"auth state {auth}"
        add("authentication", auth_state, auth_detail)
        sub = row.subscription_state if row is not None else None
        if sub == StreamSubscriptionState.REJECTED or not connected:
            sub_detail = (
                f"subscription state {sub}"
                if connected
                else f"not connected (last known subscription state {sub})"
            )
            add("subscription", DimensionState.FAILING, sub_detail)
        else:
            add(
                "subscription",
                DimensionState.OK
                if sub == StreamSubscriptionState.ACTIVE
                else DimensionState.WARNING
                if sub == StreamSubscriptionState.PARTIAL
                else DimensionState.UNKNOWN,
                f"subscription state {sub}",
            )
        if report.health is StreamHealth.STALE:
            add("message_reception", DimensionState.FAILING, "; ".join(report.reasons))
        elif report.health is StreamHealth.IDLE:
            add("message_reception", DimensionState.OK, "; ".join(report.reasons))
        elif connected:
            add(
                "message_reception",
                DimensionState.OK,
                f"last message {row.last_message_at if row else None}",
            )
        else:
            add("message_reception", DimensionState.FAILING, "not connected")
        queue_depth = row.queue_depth if row is not None else 0
        capacity = row.queue_capacity if row is not None else 0
        internal = [e for e in recent_errors if e.get("kind") == "internal"]
        if internal:
            add(
                "message_processing", DimensionState.FAILING, f"{len(internal)} processing error(s)"
            )
        elif capacity and queue_depth > 0.8 * capacity:
            add("message_processing", DimensionState.WARNING, f"backlog {queue_depth}/{capacity}")
        else:
            add("message_processing", DimensionState.OK, f"backlog {queue_depth}/{capacity}")
        critical = validation_errors.get(Severity.CRITICAL, 0)
        errors = validation_errors.get(Severity.ERROR, 0)
        add(
            "data_validation",
            DimensionState.FAILING
            if critical
            else DimensionState.WARNING
            if errors
            else DimensionState.OK,
            f"{errors} error and {critical} critical quality event(s) in the last "
            f"{int(DEGRADED_WINDOW.total_seconds() // 60)} minutes",
        )
        if metrics.get("persistence_failing"):
            add(
                "database_persistence",
                DimensionState.FAILING,
                f"writes failing since {metrics.get('persistence_failing_since')}",
            )
        elif metrics.get("last_drop_at"):
            dropped_at = datetime.fromisoformat(metrics["last_drop_at"])
            state = (
                DimensionState.WARNING if now - dropped_at < DEGRADED_WINDOW else DimensionState.OK
            )
            add("database_persistence", state, f"frames dropped at {dropped_at.isoformat()}")
        else:
            add(
                "database_persistence",
                DimensionState.OK,
                f"last write {row.last_persisted_at if row else None}",
            )

    open_total = sum(outstanding.values())
    add(
        "historical_completeness",
        DimensionState.OK if not open_total and not unavailable else DimensionState.WARNING,
        f"{open_total} outstanding gap(s), {unavailable} unavailable interval(s)",
        essential=False,
    )
    if recovery is None or not recovery.enabled:
        add("backfill_activity", inactive, "recovery worker disabled or never ran", essential=False)
    elif (
        now - recovery.heartbeat_at
        > timedelta(seconds=settings.market_data_recovery_interval) + RECOVERY_HEARTBEAT_GRACE
    ):
        add(
            "backfill_activity",
            DimensionState.FAILING,
            f"no recovery heartbeat since {recovery.heartbeat_at.isoformat()}",
            essential=False,
        )
    elif recent_failed:
        add(
            "backfill_activity",
            DimensionState.WARNING,
            f"{recent_failed} failed recovery job(s) in 24h",
            essential=False,
        )
    else:
        add(
            "backfill_activity",
            DimensionState.OK,
            f"recovery worker {recovery.state}",
            essential=False,
        )

    essential = [d for d in dims if d.essential]
    if all(d.state is DimensionState.INACTIVE for d in essential):
        overall = PipelineHealth.INACTIVE
    elif any(d.state is DimensionState.FAILING for d in essential):
        overall = PipelineHealth.FAILING
    elif any(
        d.state in (DimensionState.WARNING, DimensionState.FAILING, DimensionState.UNKNOWN)
        for d in dims
    ):
        overall = PipelineHealth.DEGRADED
    else:
        overall = PipelineHealth.HEALTHY
    return PipelineHealthResponse(
        provider=provider,
        feed=feed,
        pipeline_health=overall,
        stream_health=report.health,
        dimensions=dims,
        last_message_at=row.last_message_at if row is not None else None,
        last_persisted_at=row.last_persisted_at if row is not None else None,
        last_successful_backfill_at=last_backfill,
        outstanding_gaps=outstanding,
        pending_recovery_jobs=job_counts.get("PENDING", 0) + job_counts.get("RUNNING", 0),
        failed_recovery_jobs=job_counts.get("FAILED", 0),
        recent_provider_errors=recent_errors,
        checked_at=now,
    )


# ------------------------------------------------------------------ metrics
@router.get("/metrics")
def metrics(ctx: Context, request: Request) -> dict[str, Any]:
    """Operational metrics from the workers' persisted state (no separate monitoring
    platform): ingestion, reliability, completeness and performance."""
    provider, feed = ctx.provider.provider_id, ctx.provider.feed
    now = request.app.state.clock.now()
    with session_scope(ctx.session_factory) as session:
        row = StreamRepository(session).get_status(provider, feed)
        counters = dict(row.counters) if row is not None else {}
        stream_metrics = dict(row.metrics) if row is not None else {}
        gap_kinds = dict(
            session.execute(
                select(StreamGapRecord.kind, func.count())
                .where(StreamGapRecord.provider == provider, StreamGapRecord.feed == feed)
                .group_by(StreamGapRecord.kind)
            ).all()
        )
        sessions = session.scalar(
            select(func.count()).where(
                StreamSessionRecord.provider == provider, StreamSessionRecord.feed == feed
            )
        )
        gap_counts = CompletenessRepository(session).gap_counts(provider, feed)
        job_counts = RecoveryJobRepository(session).job_counts(provider, feed)
        attempts, durations = session.execute(
            select(
                func.coalesce(func.sum(RecoveryJobRecord.attempts), 0),
                func.avg(
                    func.extract(
                        "epoch", RecoveryJobRecord.completed_at - RecoveryJobRecord.started_at
                    )
                ),
            ).where(RecoveryJobRecord.provider == provider, RecoveryJobRecord.feed == feed)
        ).one()
        recovered = session.execute(
            select(
                MarketDataGapRecord.data_type,
                func.coalesce(func.sum(MarketDataGapRecord.records_recovered), 0),
            )
            .where(
                MarketDataGapRecord.provider == provider,
                MarketDataGapRecord.feed == feed,
            )
            .group_by(MarketDataGapRecord.data_type)
        ).all()
        recovery = RecoveryStatusRepository(session).get(provider, feed)
        ingestion_jobs = IngestionJobRepository(session).counts()
    return {
        "provider": provider,
        "feed": feed,
        "collected_at": now.isoformat(),
        "ingestion": {
            "frames_received": counters.get("frames_received", 0),
            "messages_received": counters.get("items_received", 0),
            "messages_processed": counters.get("items", 0),
            "persisted": {
                "bars": counters.get("bars_stored", 0),
                "quotes": counters.get("quotes_stored", 0),
                "trades": counters.get("trades_stored", 0),
            },
            "rejected": counters.get("records_rejected", 0),
            "quarantined": counters.get("records_quarantined", 0),
            "stale": counters.get("stale", 0),
            "duplicates": {
                "bars": counters.get("bars_duplicate", 0),
                "quotes": counters.get("quotes_duplicate", 0),
                "trades": counters.get("trades_duplicate", 0),
                "replayed_frames_suppressed": counters.get("replays_suppressed", 0),
            },
            "out_of_order": counters.get("out_of_order", 0),
            "sequence_gaps": counters.get("sequence_gaps", 0),
            "provider_event_lag_seconds": stream_metrics.get("provider_event_lag_seconds"),
            "note": "worker counters since the stream worker last started",
        },
        "reliability": {
            "stream_sessions": sessions,
            "reconnects": counters.get("reconnects", 0),
            "disconnections": gap_kinds.get(StreamGapKind.CONNECTION_LOST, 0),
            "worker_not_running_intervals": gap_kinds.get(StreamGapKind.WORKER_NOT_RUNNING, 0),
            "data_dropped_intervals": gap_kinds.get(StreamGapKind.DATA_DROPPED, 0),
            "data_not_persisted_intervals": gap_kinds.get(StreamGapKind.DATA_NOT_PERSISTED, 0),
            "database_interruptions": counters.get("database_interruptions", 0),
            "persist_failures": counters.get("persist_failures", 0),
            "frames_dropped": counters.get("frames_dropped", 0),
            "recovery_attempts": int(attempts or 0),
        },
        "completeness": {
            "gaps": gap_counts,
            "recovery_jobs": job_counts,
            "records_recovered": {dt.value: int(n) for dt, n in recovered},
            "ingestion_jobs": ingestion_jobs,
        },
        "performance": {
            "queue_depth": row.queue_depth if row is not None else None,
            "queue_capacity": row.queue_capacity if row is not None else None,
            "commit_latency_seconds": stream_metrics.get("commit_latency_seconds"),
            "persist_group_seconds": stream_metrics.get("persist_group_seconds"),
            "mean_backfill_seconds": None if durations is None else round(float(durations), 3),
            "recovery_cycle_seconds": (recovery.metrics or {}).get("cycle_seconds")
            if recovery
            else None,
            "stream_worker_heartbeat_at": row.heartbeat_at.isoformat() if row is not None else None,
            "recovery_worker_heartbeat_at": recovery.heartbeat_at.isoformat() if recovery else None,
        },
    }


__all__ = ["router"]

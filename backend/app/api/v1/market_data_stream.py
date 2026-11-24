"""Live market-data API (Deliverable 2C): stream health, sessions, gaps, quotes and trades.

All endpoints are read-only. The stream itself runs in the separate worker process
(``app.workers.market_data_stream``); these endpoints only read what it recorded in
PostgreSQL, so opening a page never opens a provider connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.market_data import Context, QueryTime, _find_asset, _market_asset
from app.core.exceptions import InvalidRequestError
from app.data_engine.capabilities import FeedCoverage
from app.data_engine.enums import (
    DataQualityStatus,
    DataSource,
    GapRecoveryStatus,
    StreamGapKind,
    StreamHealth,
    StreamSubscriptionState,
)
from app.data_engine.schemas import ConnectionState, MarketAsset
from app.data_engine.stream_health import MarketSession, StatusView, derive_health
from app.database.models import MarketQuoteRecord, MarketTradeRecord, StreamStatusRecord
from app.database.repositories import (
    AssetMappingRepository,
    LiveObservationRepository,
    StreamRepository,
)
from app.database.sessions import session_scope

router = APIRouter(prefix="/market-data", tags=["market-data"])


# ------------------------------------------------------------------ models
class StreamStatusResponse(BaseModel):
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    enabled: bool = Field(
        description="Whether the worker that last reported runs with streaming enabled; "
        "MARKET_DATA_STREAM_ENABLED of the API when no worker has reported"
    )
    health: StreamHealth
    reasons: list[str]
    market_session: MarketSession
    calendar: str
    connection_state: ConnectionState | None
    auth_state: str | None
    subscription_state: str | None
    subscribed_symbols: list[str] = Field(description="Symbols confirmed by the provider")
    active_channels: list[str] = Field(description="Channels confirmed by the provider")
    requested_subscription: dict[str, list[str]]
    confirmed_subscription: dict[str, list[str]]
    worker_id: str | None
    worker_started_at: datetime | None
    worker_stopped_at: datetime | None
    heartbeat_at: datetime | None
    current_session_id: uuid.UUID | None
    last_authenticated_at: datetime | None
    last_subscribed_at: datetime | None
    last_message_at: datetime | None
    last_persisted_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    recent_errors: list[dict[str, Any]]
    reconnect_count: int
    queue_depth: int
    queue_capacity: int
    counters: dict[str, Any]
    metrics: dict[str, Any]
    open_gaps: int
    checked_at: datetime
    notes: list[str]


class StreamSession(BaseModel):
    id: uuid.UUID
    worker_id: str
    attempt: int
    state: str
    url: str
    requested_subscription: dict[str, Any]
    confirmed_subscription: dict[str, Any]
    started_at: datetime
    authenticated_at: datetime | None
    subscribed_at: datetime | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    last_persisted_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    counters: dict[str, Any]


class StreamGap(BaseModel):
    id: int
    kind: StreamGapKind
    gap_start: datetime
    gap_end: datetime | None
    last_message_before: datetime | None
    reason: str
    symbols: list[str]
    channels: list[str]
    previous_session_id: uuid.UUID | None
    recovery_session_id: uuid.UUID | None
    frames_dropped: int
    recovery_status: GapRecoveryStatus
    detected_at: datetime
    closed_at: datetime | None


class _LiveRow(BaseModel):
    id: int
    provider_symbol: str
    event_time: datetime
    event_time_ns: int = Field(description="Exact provider timestamp, ns since the Unix epoch")
    conditions: list[str]
    tape: str | None
    source: DataSource
    quality_status: DataQualityStatus
    received_at: datetime
    available_at: datetime
    original_available_at: datetime | None
    processed_at: datetime
    ingested_at: datetime
    raw_event_id: int | None


class Quote(_LiveRow):
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    bid_exchange: str | None
    ask_exchange: str | None


class Trade(_LiveRow):
    price: Decimal
    size: Decimal | None
    provider_trade_id: str | None
    exchange: str | None


class _LiveResponse(BaseModel):
    asset: MarketAsset
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    quality: list[DataQualityStatus]
    next_after_id: int | None = Field(description="Pass as `after_id` for the next page")
    notes: list[str]


class QuotesResponse(_LiveResponse):
    quotes: list[Quote]


class TradesResponse(_LiveResponse):
    trades: list[Trade]


# ------------------------------------------------------------------ helpers
def _view(row: StreamStatusRecord) -> StatusView:
    return StatusView(
        enabled=row.enabled,
        connection_state=ConnectionState(row.connection_state),
        subscription_state=StreamSubscriptionState(row.subscription_state),
        heartbeat_at=row.heartbeat_at,
        last_subscribed_at=row.last_subscribed_at,
        last_message_at=row.last_message_at,
        last_persisted_at=row.last_persisted_at,
        metrics=row.metrics or {},
    )


def _feed_notes(ctx: Context) -> list[str]:
    access = ctx.provider.feed_access()
    notes = [f"Feed '{ctx.provider.feed}' only; Atlas never switches feeds."]
    if access.coverage is FeedCoverage.SINGLE_VENUE:
        notes.append(
            f"'{ctx.provider.feed}' is a single venue: its quotes, trades and bars do not "
            "represent consolidated US market activity."
        )
    return notes


def _range(start: datetime, end: datetime) -> None:
    if end < start:
        raise InvalidRequestError("end must not be before start")
    if end - start > timedelta(days=7):
        raise InvalidRequestError("ranges are limited to 7 days; page with after_id")


# ------------------------------------------------------------------ endpoints
@router.get("/stream", response_model=StreamStatusResponse)
def stream_status(ctx: Context, request: Request) -> StreamStatusResponse:
    """Health of the live stream for the configured provider/feed.

    ``health`` is derived, never assumed: a connected stream that stopped receiving or
    persisting data is not reported HEALTHY, and a worker without a recent heartbeat is
    NOT_RUNNING whatever its last recorded state.
    """
    provider, feed = ctx.provider.provider_id, ctx.provider.feed
    now = request.app.state.clock.now()
    stale_after = timedelta(seconds=ctx.settings.market_data_stream_stale_after)
    with session_scope(ctx.session_factory) as session:
        repo = StreamRepository(session)
        row = repo.get_status(provider, feed)
        open_gaps = len(repo.gaps(provider, feed, open_only=True, limit=1000))
        report = derive_health(
            _view(row) if row else None,
            enabled=ctx.settings.market_data_stream_enabled,
            now=now,
            stale_after=stale_after,
        )
        confirmed: dict[str, list[str]] = dict(row.confirmed_subscription) if row else {}
        if report.health in (StreamHealth.NOT_RUNNING, StreamHealth.RECONNECTING):
            # The last confirmation no longer describes a live subscription.
            active: dict[str, list[str]] = {}
        else:
            active = confirmed
        symbols = sorted({s for values in active.values() for s in values})
        notes = _feed_notes(ctx)
        notes.append(
            "Stream gaps are interruptions of live collection. Whether data is missing and "
            "whether it was recovered is reported by /market-data/gaps and /completeness."
        )
        return StreamStatusResponse(
            provider=provider,
            feed=feed,
            feed_coverage=ctx.provider.feed_access().coverage,
            # The worker may run with its own configuration (e.g. an environment override).
            enabled=row.enabled if row else ctx.settings.market_data_stream_enabled,
            health=report.health,
            reasons=list(report.reasons),
            market_session=report.session,
            calendar=report.calendar,
            connection_state=ConnectionState(row.connection_state) if row else None,
            auth_state=row.auth_state if row else None,
            subscription_state=row.subscription_state if row else None,
            subscribed_symbols=symbols,
            active_channels=sorted(active),
            requested_subscription=dict(row.requested_subscription) if row else {},
            confirmed_subscription=confirmed,
            worker_id=row.worker_id if row else None,
            worker_started_at=row.worker_started_at if row else None,
            worker_stopped_at=row.worker_stopped_at if row else None,
            heartbeat_at=row.heartbeat_at if row else None,
            current_session_id=row.current_session_id if row else None,
            last_authenticated_at=row.last_authenticated_at if row else None,
            last_subscribed_at=row.last_subscribed_at if row else None,
            last_message_at=row.last_message_at if row else None,
            last_persisted_at=row.last_persisted_at if row else None,
            last_error=row.last_error if row else None,
            last_error_at=row.last_error_at if row else None,
            recent_errors=list(row.recent_errors) if row else [],
            reconnect_count=row.reconnect_count if row else 0,
            queue_depth=row.queue_depth if row else 0,
            queue_capacity=row.queue_capacity if row else 0,
            counters=dict(row.counters) if row else {},
            metrics=dict(row.metrics) if row else {},
            open_gaps=open_gaps,
            checked_at=now,
            notes=notes,
        )


@router.get("/stream/sessions", response_model=list[StreamSession])
def stream_sessions(
    ctx: Context,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StreamSession]:
    """Connection attempts, newest first."""
    with session_scope(ctx.session_factory) as session:
        rows = StreamRepository(session).sessions(
            ctx.provider.provider_id, ctx.provider.feed, limit=limit, offset=offset
        )
        return [StreamSession.model_validate(r, from_attributes=True) for r in rows]


@router.get("/stream/gaps", response_model=list[StreamGap])
def stream_gaps(
    ctx: Context,
    open_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StreamGap]:
    """Intervals in which live data may be missing (disconnects, worker downtime, drops)."""
    with session_scope(ctx.session_factory) as session:
        rows = StreamRepository(session).gaps(
            ctx.provider.provider_id,
            ctx.provider.feed,
            open_only=open_only,
            limit=limit,
            offset=offset,
        )
        return [StreamGap.model_validate(r, from_attributes=True) for r in rows]


@router.get("/quotes", response_model=QuotesResponse)
def quotes(
    ctx: Context,
    start: QueryTime,
    end: QueryTime,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    quality: Annotated[list[DataQualityStatus] | None, Query()] = None,
    after_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
    newest_first: Annotated[
        bool, Query(description="Latest `limit` stored rows, newest first (no paging)")
    ] = False,
) -> QuotesResponse:
    """Stored live quotes for one asset on the configured feed, in arrival order."""
    _range(start, end)
    if newest_first and after_id is not None:
        raise InvalidRequestError("newest_first does not page; omit after_id")
    statuses = quality or [DataQualityStatus.VALID]
    provider = ctx.provider
    with session_scope(ctx.session_factory) as session:
        asset = _find_asset(session, symbol.upper() if symbol else None, asset_id)
        mapping = AssetMappingRepository(session).current_for_asset(asset.id, provider.provider_id)
        rows: list[MarketQuoteRecord] = list(
            LiveObservationRepository(session).quotes(
                asset.id,
                provider.feed,
                start=start,
                end=end,
                limit=limit,
                after_id=after_id,
                newest_first=newest_first,
            )
        )
        notes = _feed_notes(ctx)
        notes.append("Quote sizes are in the provider's units (Alpaca documents round lots).")
        return QuotesResponse(
            asset=_market_asset(
                asset, provider.provider_id, mapping.provider_symbol if mapping else None
            ),
            provider=provider.provider_id,
            feed=provider.feed,
            feed_coverage=provider.feed_access().coverage,
            quality=statuses,
            quotes=[
                Quote.model_validate(r, from_attributes=True)
                for r in rows
                if r.quality_status in statuses
            ],
            next_after_id=rows[-1].id if len(rows) == limit and not newest_first else None,
            notes=notes,
        )


@router.get("/trades", response_model=TradesResponse)
def trades(
    ctx: Context,
    start: QueryTime,
    end: QueryTime,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    quality: Annotated[list[DataQualityStatus] | None, Query()] = None,
    after_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
    newest_first: Annotated[
        bool, Query(description="Latest `limit` stored rows, newest first (no paging)")
    ] = False,
) -> TradesResponse:
    """Stored live market trades (exchange prints) for one asset. Read-only market data:
    Atlas has no order or execution endpoints."""
    _range(start, end)
    if newest_first and after_id is not None:
        raise InvalidRequestError("newest_first does not page; omit after_id")
    statuses = quality or [DataQualityStatus.VALID]
    provider = ctx.provider
    with session_scope(ctx.session_factory) as session:
        asset = _find_asset(session, symbol.upper() if symbol else None, asset_id)
        mapping = AssetMappingRepository(session).current_for_asset(asset.id, provider.provider_id)
        rows: list[MarketTradeRecord] = list(
            LiveObservationRepository(session).trades(
                asset.id,
                provider.feed,
                start=start,
                end=end,
                limit=limit,
                after_id=after_id,
                newest_first=newest_first,
            )
        )
        notes = _feed_notes(ctx)
        notes.append(
            "Trade corrections and cancellations are recorded as quality events "
            "(CORRECTION / TRADE_CANCEL); stored trades are never modified."
        )
        return TradesResponse(
            asset=_market_asset(
                asset, provider.provider_id, mapping.provider_symbol if mapping else None
            ),
            provider=provider.provider_id,
            feed=provider.feed,
            feed_coverage=provider.feed_access().coverage,
            quality=statuses,
            trades=[
                Trade.model_validate(r, from_attributes=True)
                for r in rows
                if r.quality_status in statuses
            ],
            next_after_id=rows[-1].id if len(rows) == limit and not newest_first else None,
            notes=notes,
        )

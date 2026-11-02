"""Dashboard read models (Deliverable 2E): market status, per-asset snapshots, the
non-secret market-data configuration and the market-data audit trail.

All read-only and computed from PostgreSQL and the backend calendar; nothing here calls
the provider. No response contains a credential value: credentials are reported only as
a status (NOT_CONFIGURED, CONFIGURED, VERIFIED, REJECTED).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.market_data import Context, _market_asset
from app.core.exceptions import InvalidRequestError
from app.core.security import TRADING_MODE
from app.data_engine.calendar import VERIFIED_YEARS
from app.data_engine.capabilities import FeedCoverage
from app.data_engine.enums import DataType
from app.data_engine.market_overview import (
    AssetSnapshot,
    MarketStatus,
    SnapshotSettings,
    build_snapshot,
    market_status,
    monitored_assets,
    open_gap_counts,
    quality_event_counts,
)
from app.data_engine.schemas import CredentialStatus
from app.database.repositories import AssetMappingRepository, AuditLogRepository, StreamRepository
from app.database.sessions import session_scope

router = APIRouter(prefix="/market-data", tags=["market-data"])

AUDIT_PREFIX = "market_data."
QUALITY_WINDOW = timedelta(hours=24)
# Each snapshot costs several queries in the request's transaction: keep requests bounded.
MAX_SNAPSHOT_SYMBOLS = 50
# Timeframes the dashboard charts offer (all supported by the provider's history API).
DASHBOARD_TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")


# ------------------------------------------------------------------ models
class SnapshotsResponse(BaseModel):
    provider: str
    feed: str
    feed_coverage: FeedCoverage
    market: MarketStatus
    snapshots: list[AssetSnapshot]
    checked_at: datetime
    notes: list[str]


class StreamConfiguration(BaseModel):
    enabled_in_api: bool = Field(description="MARKET_DATA_STREAM_ENABLED of the API process")
    enabled_in_worker: bool | None = Field(
        description="As reported by the stream worker (it may run with its own environment)"
    )
    channels: list[str]
    stale_after_seconds: float
    heartbeat_timeout_seconds: float
    queue_size: int


class RecoveryConfiguration(BaseModel):
    enabled: bool
    interval_seconds: float
    timeframes: list[str]
    data_types: list[str]
    lookback_days: int
    settle_seconds: float
    max_attempts: int
    trade_reconciliation_minutes: int


class MarketDataConfiguration(BaseModel):
    environment: str
    trading_mode: str
    provider: str
    provider_display_name: str
    feed: str
    feed_display_name: str
    feed_description: str
    feed_coverage: FeedCoverage
    feed_delay_seconds: float
    adjustment: str
    credentials: CredentialStatus = Field(description="Status only; values are never exposed")
    operator_token_configured: bool = Field(
        description="Whether operational actions require X-Atlas-Operator-Token"
    )
    configured_symbols: list[str]
    dashboard_timeframes: list[str]
    historical_timeframes: dict[str, list[int]] = Field(
        description="Amounts the provider's history API accepts, per unit"
    )
    stream_bar_timeframes: list[str]
    stale_observation_seconds: float
    stream: StreamConfiguration
    recovery: RecoveryConfiguration
    calendar_verified_years: list[int]
    limitations: list[str]


class AuditEvent(BaseModel):
    id: int
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    priority: str | None
    entity_type: str | None
    entity_id: str | None
    reason: str | None
    details: dict[str, Any]


# ------------------------------------------------------------------ endpoints
@router.get("/market-status", response_model=MarketStatus)
def get_market_status(ctx: Context, request: Request) -> MarketStatus:
    """NYSE session now, today's hours, the next regular session and whether the
    configured feed publishes now (from the backend calendar)."""
    now = request.app.state.clock.now()
    return market_status(now, ctx.provider.feed, ctx.provider.feed_access().session_hours)


@router.get("/snapshots", response_model=SnapshotsResponse)
def snapshots(
    ctx: Context,
    request: Request,
    symbol: Annotated[
        list[str] | None,
        Query(description=f"These mapped assets (at most {MAX_SNAPSHOT_SYMBOLS})"),
    ] = None,
) -> SnapshotsResponse:
    """Latest stored price, quote, bar, previous close and freshness per monitored asset
    (``MARKET_DATA_SYMBOLS`` plus the stream worker's symbols), or per requested symbol."""
    wanted = {s.strip().upper() for s in symbol} if symbol else None
    if wanted is not None and len(wanted) > MAX_SNAPSHOT_SYMBOLS:
        raise InvalidRequestError(f"at most {MAX_SNAPSHOT_SYMBOLS} symbols per request")
    now = request.app.state.clock.now()
    provider = ctx.provider
    access = provider.feed_access()
    feed_capability = provider.capabilities().feed(provider.feed)
    config = SnapshotSettings(
        provider=provider.provider_id,
        feed=provider.feed,
        feed_coverage=access.coverage,
        delay=feed_capability.delay,
        adjustment=ctx.settings.historical_data_adjustment,
        hours=access.session_hours,
        stale_after=timedelta(seconds=ctx.settings.market_data_stream_stale_after),
    )
    with session_scope(ctx.session_factory) as session:
        mapped = (
            AssetMappingRepository(session).current_assets(provider.provider_id, symbols=wanted)
            if wanted is not None
            else monitored_assets(
                session, provider.provider_id, provider.feed, ctx.settings.market_data_symbols
            )
        )
        status_row = StreamRepository(session).get_status(provider.provider_id, provider.feed)
        subscription = status_row.confirmed_subscription if status_row is not None else {}
        confirmed = {s for values in (subscription or {}).values() for s in values}
        quality = quality_event_counts(session, provider.provider_id, now - QUALITY_WINDOW)
        gaps = open_gap_counts(session, provider.provider_id, provider.feed)
        result = [
            build_snapshot(
                session,
                _market_asset(asset, provider.provider_id, mapping.provider_symbol),
                config,
                now,
                subscribed=mapping.provider_symbol in confirmed,
                quality_events=quality.get(asset.id, {}),
                open_gaps=gaps.get(asset.id, 0),
            )
            for asset, mapping in mapped
        ]
    notes = [
        "Last price: the newest VALID trade, or the close of the newest VALID 1-minute bar "
        "when that is newer. Stored data only; nothing is estimated.",
        "Change is computed only against a stored, VALID daily bar of the previous trading "
        "day; otherwise it is omitted.",
    ]
    if access.coverage is FeedCoverage.SINGLE_VENUE:
        notes.append(
            f"Feed '{provider.feed}' is a single venue: prices, volumes and daily bars do not "
            "represent consolidated US market activity."
        )
    return SnapshotsResponse(
        provider=provider.provider_id,
        feed=provider.feed,
        feed_coverage=access.coverage,
        market=market_status(now, provider.feed, access.session_hours),
        snapshots=result,
        checked_at=now,
        notes=notes,
    )


@router.get("/configuration", response_model=MarketDataConfiguration)
def configuration(ctx: Context, request: Request) -> MarketDataConfiguration:
    """Effective, non-secret market-data configuration. Read-only: changing it means
    changing the environment and restarting the processes (no runtime switch)."""
    settings = request.app.state.settings
    md = ctx.settings
    provider = ctx.provider
    capabilities = provider.capabilities()
    feed = capabilities.feed(provider.feed)
    access = provider.feed_access()
    with session_scope(ctx.session_factory) as session:
        row = StreamRepository(session).get_status(provider.provider_id, provider.feed)
        worker_enabled = row.enabled if row is not None else None
    limitations = list(access.notes)
    enabled_types = set(md.market_data_recovery_data_types)
    missing = [f"{d.value.lower()}s" for d in DataType if d not in enabled_types]
    if missing:
        limitations.append(
            "Automatic recovery is not enabled for: " + ", ".join(missing) + " "
            "(MARKET_DATA_RECOVERY_DATA_TYPES); their gaps stay listed as missing."
        )
    limitations.append(
        "The live stream publishes 1-minute bars only; other timeframes come from stored "
        "provider history."
    )
    return MarketDataConfiguration(
        environment=settings.environment.value,
        trading_mode=TRADING_MODE.value,
        provider=provider.provider_id,
        provider_display_name=capabilities.display_name,
        feed=provider.feed,
        feed_display_name=feed.display_name,
        feed_description=feed.description,
        feed_coverage=feed.coverage,
        feed_delay_seconds=feed.delay.total_seconds(),
        adjustment=md.historical_data_adjustment.value,
        credentials=provider.credential_status(),
        operator_token_configured=settings.operator_token is not None,
        configured_symbols=list(md.market_data_symbols),
        dashboard_timeframes=list(DASHBOARD_TIMEFRAMES),
        historical_timeframes={
            rule.unit.value: sorted(rule.amounts) for rule in capabilities.historical_timeframes
        },
        stream_bar_timeframes=sorted(str(tf) for tf in capabilities.streaming_bar_timeframes),
        stale_observation_seconds=md.market_data_stale_observation_seconds,
        stream=StreamConfiguration(
            enabled_in_api=md.market_data_stream_enabled,
            enabled_in_worker=worker_enabled,
            channels=[c.value for c in md.market_data_stream_channels],
            stale_after_seconds=md.market_data_stream_stale_after,
            heartbeat_timeout_seconds=md.market_data_heartbeat_timeout,
            queue_size=md.market_data_stream_queue_size,
        ),
        recovery=RecoveryConfiguration(
            enabled=md.market_data_recovery_enabled,
            interval_seconds=md.market_data_recovery_interval,
            timeframes=list(md.market_data_recovery_timeframes),
            data_types=[d.value for d in md.market_data_recovery_data_types],
            lookback_days=md.market_data_completeness_lookback_days,
            settle_seconds=md.market_data_recovery_settle_seconds,
            max_attempts=md.market_data_recovery_max_attempts,
            trade_reconciliation_minutes=md.market_data_trade_reconciliation_minutes,
        ),
        calendar_verified_years=list(VERIFIED_YEARS),
        limitations=limitations,
    )


@router.get("/audit-events", response_model=list[AuditEvent])
def audit_events(
    ctx: Context,
    priority: Annotated[str | None, Query(pattern="^(NORMAL|HIGH)$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: int | None = None,
) -> list[AuditEvent]:
    """Market-data audit trail (outages, recoveries, unavailable data), newest first;
    page with ``before_id``. HIGH marks genuine integrity problems."""
    with session_scope(ctx.session_factory) as session:
        rows = AuditLogRepository(session).list_by_prefix(
            AUDIT_PREFIX, limit=limit, before_id=before_id, priority=priority
        )
        return [
            AuditEvent(
                id=row.id,
                occurred_at=row.occurred_at,
                actor_type=row.actor_type.value,
                actor_id=row.actor_id,
                action=row.action,
                priority=(row.details or {}).get("priority"),
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                reason=row.reason,
                details=dict(row.details or {}),
            )
            for row in rows
        ]


__all__ = ["router"]

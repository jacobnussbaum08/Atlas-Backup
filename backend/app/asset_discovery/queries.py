"""Read models for the asset discovery API and dashboard (Phase 3A).

Everything here reads stored records; nothing calls a provider. "Latest" evaluation and
score mean those of the newest successful live qualification and scoring jobs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app.asset_discovery import jobs, membership
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    LifecycleEventType,
    MembershipAction,
    QualificationOutcome,
    UniverseLayer,
)
from app.database.models import (
    Asset,
    AssetReferenceVersionRecord,
    AssetStatus,
    DiscoveryJobRecord,
    ProviderAssetRecord,
    QualificationEvaluationRecord,
    UniverseMembershipEventRecord,
    WatchlistScoreRecord,
)

SortKey = Literal[
    "symbol",
    "name",
    "exchange",
    "status",
    "first_discovered_at",
    "last_verified_at",
    "rank",
    "score",
]


@dataclass(frozen=True)
class AssetFilter:
    search: str | None = None
    layer: UniverseLayer | None = None
    statuses: Sequence[AssetStatus] = ()
    exchanges: Sequence[str] = ()
    asset_classes: Sequence[str] = ()
    instrument_types: Sequence[str] = ()
    outcome: QualificationOutcome | None = None
    tradable: bool | None = None
    shortable: bool | None = None


def latest_job_id(session: Session, kind: DiscoveryJobKind, provider: str) -> uuid.UUID | None:
    job = jobs.last_finished(session, kind, provider, successful_only=True)
    if job is None:
        return None
    if kind is DiscoveryJobKind.QUALIFICATION and not job.summary.get("live", True):
        # Historical reconstructions are not "the current" evaluation.
        found = session.scalar(
            select(DiscoveryJobRecord.id)
            .where(
                DiscoveryJobRecord.kind == kind,
                DiscoveryJobRecord.provider == provider,
                DiscoveryJobRecord.summary["live"].as_boolean().is_(True),
                DiscoveryJobRecord.status.in_(jobs.FINISHED[:2]),
            )
            .order_by(DiscoveryJobRecord.completed_at.desc())
            .limit(1)
        )
        return found
    return job.id


def _latest_events(layer: UniverseLayer) -> Any:
    e = UniverseMembershipEventRecord
    return (
        select(e.asset_id, e.action, e.effective_at)
        .where(e.layer == layer)
        .ext(distinct_on(e.asset_id))
        .order_by(e.asset_id, e.sequence.desc())
        .subquery()
    )


def asset_rows(
    session: Session,
    provider: str,
    flt: AssetFilter,
    *,
    sort: SortKey,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    pa = ProviderAssetRecord
    v = AssetReferenceVersionRecord
    pool, qual, watch = (_latest_events(layer) for layer in UniverseLayer)
    qual_job = latest_job_id(session, DiscoveryJobKind.QUALIFICATION, provider)
    score_job = latest_job_id(session, DiscoveryJobKind.WATCHLIST_SCORING, provider)
    ev = aliased(QualificationEvaluationRecord)
    sc = aliased(WatchlistScoreRecord)
    stmt: Any = (
        select(
            Asset,
            pa.provider_asset_id,
            pa.current_symbol,
            v.tradable,
            v.shortable,
            v.easy_to_borrow,
            v.marginable,
            v.fractionable,
            v.provider_exchange,
            v.attributes,
            pool.c.action.label("pool_action"),
            qual.c.action.label("qual_action"),
            qual.c.effective_at.label("qual_since"),
            watch.c.action.label("watch_action"),
            ev.outcome,
            ev.failed_rules,
            ev.unavailable_rules,
            sc.rank,
            sc.overall_score,
        )
        .join(pa, and_(pa.asset_id == Asset.id, pa.provider == provider), isouter=True)
        .join(v, and_(v.provider_asset_ref == pa.id, v.version == pa.current_version), isouter=True)
        .join(pool, pool.c.asset_id == Asset.id, isouter=True)
        .join(qual, qual.c.asset_id == Asset.id, isouter=True)
        .join(watch, watch.c.asset_id == Asset.id, isouter=True)
        .join(ev, and_(ev.asset_id == Asset.id, ev.job_id == qual_job), isouter=True)
        .join(sc, and_(sc.asset_id == Asset.id, sc.job_id == score_job), isouter=True)
    )
    if flt.search:
        term = f"%{flt.search.strip()}%"
        stmt = stmt.where(or_(Asset.symbol.ilike(term), Asset.name.ilike(term)))
    layer_col = {
        UniverseLayer.DISCOVERY_POOL: pool.c.action,
        UniverseLayer.QUALIFIED_UNIVERSE: qual.c.action,
        UniverseLayer.ACTIVE_WATCHLIST: watch.c.action,
    }
    if flt.layer is not None:
        stmt = stmt.where(layer_col[flt.layer] == MembershipAction.ADDED)
    if flt.statuses:
        stmt = stmt.where(Asset.status.in_(list(flt.statuses)))
    if flt.exchanges:
        stmt = stmt.where(Asset.exchange_code.in_(list(flt.exchanges)))
    if flt.asset_classes:
        stmt = stmt.where(Asset.asset_class.in_(list(flt.asset_classes)))
    if flt.instrument_types:
        stmt = stmt.where(Asset.instrument_type.in_(list(flt.instrument_types)))
    if flt.outcome is not None:
        stmt = stmt.where(ev.outcome == flt.outcome)
    if flt.tradable is not None:
        stmt = stmt.where(v.tradable.is_(flt.tradable))
    if flt.shortable is not None:
        stmt = stmt.where(v.shortable.is_(flt.shortable))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    column = {
        "symbol": Asset.symbol,
        "name": Asset.name,
        "exchange": Asset.exchange_code,
        "status": Asset.status,
        "first_discovered_at": Asset.first_discovered_at,
        "last_verified_at": Asset.last_verified_at,
        "rank": sc.rank,
        "score": sc.overall_score,
    }[sort]
    order = column.desc().nulls_last() if descending else column.asc().nulls_last()
    rows = session.execute(stmt.order_by(order, Asset.symbol, Asset.id).limit(limit).offset(offset))
    items = []
    for r in rows:
        asset: Asset = r[0]
        items.append(
            {
                "asset_id": asset.id,
                "symbol": asset.symbol,
                "name": asset.name,
                "asset_class": asset.asset_class,
                "instrument_type": asset.instrument_type,
                "exchange": asset.exchange_code,
                "provider_exchange": r.provider_exchange,
                "status": asset.status,
                "provider_asset_id": r.provider_asset_id,
                "provider_symbol": r.current_symbol,
                "tradable": r.tradable,
                "shortable": r.shortable,
                "easy_to_borrow": r.easy_to_borrow,
                "marginable": r.marginable,
                "fractionable": r.fractionable,
                "attributes": list(r.attributes or []),
                "first_discovered_at": asset.first_discovered_at,
                "last_verified_at": asset.last_verified_at,
                "in_discovery_pool": r.pool_action is MembershipAction.ADDED,
                "qualified": r.qual_action is MembershipAction.ADDED,
                "qualified_since": r.qual_since
                if r.qual_action is MembershipAction.ADDED
                else None,
                "on_watchlist": r.watch_action is MembershipAction.ADDED,
                "qualification_outcome": r.outcome,
                "failed_rules": list(r.failed_rules or []),
                "unavailable_rules": list(r.unavailable_rules or []),
                "watchlist_rank": r.rank,
                "watchlist_score": r.overall_score,
            }
        )
    return int(total), items


def summary(session: Session, provider: str, now: datetime, recent_days: int = 7) -> dict[str, Any]:
    since = now - timedelta(days=recent_days)
    counts = dict(session.execute(select(Asset.status, func.count()).group_by(Asset.status)).all())
    catalogued = session.scalar(
        select(func.count())
        .select_from(ProviderAssetRecord)
        .where(ProviderAssetRecord.provider == provider)
    )
    layers = {
        layer.value: len(membership.current_members(session, layer)) for layer in UniverseLayer
    }
    e = UniverseMembershipEventRecord
    from app.database.models import AssetLifecycleEventRecord as L

    new_listings = session.scalar(
        select(func.count())
        .select_from(L)
        .where(L.event_type == LifecycleEventType.NEW_LISTING, L.effective_at >= since)
    )
    disqualified = session.scalar(
        select(func.count())
        .select_from(e)
        .where(
            e.layer == UniverseLayer.QUALIFIED_UNIVERSE,
            e.action == MembershipAction.REMOVED,
            e.effective_at >= since,
        )
    )
    deactivated = session.scalar(
        select(func.count())
        .select_from(L)
        .where(
            L.event_type.in_([LifecycleEventType.DEACTIVATED, LifecycleEventType.DELISTED]),
            L.effective_at >= since,
        )
    )
    last_jobs = {}
    for kind in DiscoveryJobKind:
        job = session.scalar(
            select(DiscoveryJobRecord)
            .where(DiscoveryJobRecord.kind == kind, DiscoveryJobRecord.provider == provider)
            .order_by(DiscoveryJobRecord.created_at.desc())
            .limit(1)
        )
        last_jobs[kind.value] = job
    last_success = jobs.last_finished(
        session, DiscoveryJobKind.DIRECTORY_DISCOVERY, provider, successful_only=True
    )
    return {
        "catalogued": int(catalogued or 0),
        "by_status": {k.value: int(v) for k, v in counts.items()},
        "layers": layers,
        "recent_days": recent_days,
        "new_listings": int(new_listings or 0),
        "recently_disqualified": int(disqualified or 0),
        "recently_deactivated": int(deactivated or 0),
        "last_jobs": last_jobs,
        "last_successful_discovery": last_success,
    }


__all__ = ["AssetFilter", "SortKey", "asset_rows", "latest_job_id", "summary"]

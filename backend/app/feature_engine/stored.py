"""Features read from stored Phase 3 results (Phase 4A): market-intelligence regimes and
news events.

Both are point in time. A row is an input only if it was available by the knowledge time
(``known_at``) and describes the period up to the data cutoff; with basis RECORDED,
``known_at`` is the cutoff itself.

News coverage is explicit: a count of zero articles means "nothing was published" only if
Atlas was retrieving news for the whole window (a completed retrieval job covering it, or a
subscribed real-time stream). Otherwise the value is ``UNAVAILABLE``: missing news coverage
is never reported as an absence of news.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.database.models import (
    Asset,
    EventAssetRelationshipRecord,
    EventClassificationRecord,
    NewsEventRecord,
    NewsIngestionJobRecord,
    NewsStreamSessionRecord,
    NewsStreamStatusRecord,
    RegimeClassificationRecord,
)
from app.feature_engine.registry import (
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ResolvedFeature,
)
from app.market_intelligence.enums import KnowledgeBasis, Scope
from app.news.enums import AssetRelationship, NewsJobStatus

# A stream whose status heartbeat is older than this is not treated as still covering.
STREAM_STALE_AFTER = timedelta(minutes=2)


@dataclass(frozen=True)
class StoredContext:
    asset_id: uuid.UUID
    data_cutoff: datetime
    known_at: datetime
    basis: KnowledgeBasis


def compute_stored_features(
    session: Session, features: Sequence[ResolvedFeature], ctx: StoredContext
) -> dict[str, FeatureValue]:
    values: dict[str, FeatureValue] = {}
    symbol: str | None = None
    for feature in features:
        source = feature.definition.source
        if source is FeatureSource.MARKET_INTELLIGENCE:
            values[feature.key] = _regime(session, feature, ctx)
        elif source is FeatureSource.NEWS:
            if symbol is None:
                symbol = session.scalar(select(Asset.symbol).where(Asset.id == ctx.asset_id))
            values[feature.key] = _news(session, feature, ctx, symbol or "")
        else:
            raise ValueError(f"{feature.definition.name} is not a stored feature")
    return values


def _value(
    feature: ResolvedFeature,
    status: FeatureStatus,
    reason: str | None,
    value: float | str | None = None,
    available_at: datetime | None = None,
    inputs: dict[str, Any] | None = None,
) -> FeatureValue:
    d = feature.definition
    return FeatureValue(
        key=feature.key,
        name=d.name,
        version=d.version,
        source=d.source,
        timeframe=feature.timeframe,
        params=feature.param_map,
        value=value,
        status=status,
        reason=reason,
        available_at=available_at,
        inputs=inputs or {},
    )


def _regime(session: Session, feature: ResolvedFeature, ctx: StoredContext) -> FeatureValue:
    p = feature.param_map
    scope = Scope(str(p["scope"]))
    r = RegimeClassificationRecord
    stmt = (
        select(r)
        .where(
            r.scope == scope,
            r.timeframe == feature.timeframe,
            r.dimension == p["dimension"],
            r.as_of <= ctx.data_cutoff,
            r.available_at <= ctx.known_at,
        )
        .order_by(r.as_of.desc(), r.available_at.desc(), r.id.desc())
        .limit(1)
    )
    stmt = stmt.where(r.asset_id == ctx.asset_id if scope is Scope.ASSET else r.asset_id.is_(None))
    if ctx.basis is KnowledgeBasis.RECORDED:
        stmt = stmt.where(r.basis == KnowledgeBasis.RECORDED)
    row = session.scalar(stmt)
    if row is None:
        return _value(feature, FeatureStatus.UNAVAILABLE, "no stored classification by the cutoff")
    age = ctx.data_cutoff - row.as_of
    inputs = {
        "run_id": str(row.run_id),
        "as_of": row.as_of.isoformat(),
        "algorithm": row.algorithm,
        "quality": row.quality.value,
        "basis": row.basis.value,
    }
    max_age = timedelta(hours=int(p["max_age_hours"]))
    if age > max_age:
        return _value(
            feature,
            FeatureStatus.UNAVAILABLE,
            f"newest classification is {age.total_seconds() / 3600:.0f} h old "
            f"(limit {max_age.total_seconds() / 3600:.0f} h)",
            None,
            row.available_at,
            inputs,
        )
    value: float | str = (
        row.label if feature.definition.name == "regime_label" else float(row.confidence)
    )
    return _value(feature, FeatureStatus.VALID, None, value, row.available_at, inputs)


# --- News -------------------------------------------------------------------------------


def coverage_intervals(
    session: Session, symbol: str, *, known_at: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Time ranges for which Atlas was retrieving news that includes ``symbol``, as known
    at ``known_at``: completed retrieval jobs (all symbols or including it) and subscribed
    real-time stream sessions (``*`` or the symbol)."""
    intervals: list[tuple[datetime, datetime, str]] = []
    j = NewsIngestionJobRecord
    jobs = session.execute(
        select(j.range_start, j.range_end, j.kind).where(
            j.status == NewsJobStatus.COMPLETED,
            j.completed_at.is_not(None),
            j.completed_at <= known_at,
            or_(func.cardinality(j.symbols) == 0, j.symbols.contains([symbol])),
        )
    )
    intervals.extend((row.range_start, row.range_end, row.kind.value) for row in jobs)
    s = NewsStreamSessionRecord
    status = {
        row.current_session_id: row.heartbeat_at
        for row in session.execute(
            select(NewsStreamStatusRecord.current_session_id, NewsStreamStatusRecord.heartbeat_at)
        )
    }
    streams = session.execute(
        select(s.id, s.subscribed_at, s.ended_at, s.last_message_at).where(
            s.subscribed_at.is_not(None),
            s.subscribed_at <= known_at,
            or_(s.confirmed_symbols.contains(["*"]), s.confirmed_symbols.contains([symbol])),
        )
    )
    for row in streams:
        if row.ended_at is not None:
            end = row.ended_at
        elif (beat := status.get(row.id)) is not None and beat >= known_at - STREAM_STALE_AFTER:
            end = known_at  # the current, healthy session
        else:
            end = row.last_message_at or row.subscribed_at
        intervals.append((row.subscribed_at, min(end, known_at), "STREAM"))
    return intervals


def covered(
    intervals: Sequence[tuple[datetime, datetime, str]], start: datetime, end: datetime
) -> bool:
    """Whether the union of ``intervals`` contains ``[start, end]``."""
    reach = start
    for lo, hi, _ in sorted(intervals):
        if lo > reach:
            break
        reach = max(reach, hi)
        if reach >= end:
            return True
    return reach >= end


def _news(
    session: Session, feature: ResolvedFeature, ctx: StoredContext, symbol: str
) -> FeatureValue:
    p = feature.param_map
    start = ctx.data_cutoff - timedelta(hours=int(p["lookback_hours"]))
    intervals = coverage_intervals(session, symbol, known_at=ctx.known_at)
    coverage_inputs: dict[str, Any] = {
        "window_start": start.isoformat(),
        "window_end": ctx.data_cutoff.isoformat(),
        "coverage_sources": sorted({kind for _, _, kind in intervals}),
    }
    if not covered(intervals, start, ctx.data_cutoff):
        return _value(
            feature,
            FeatureStatus.UNAVAILABLE,
            "news retrieval did not cover the whole window; absence of articles is unknown",
            inputs=coverage_inputs,
        )
    e, rel = NewsEventRecord, EventAssetRelationshipRecord
    # The newest event per article known at known_at (a reclassification or a later
    # revision supersedes earlier events only once it was available).
    newest = (
        select(e.id)
        .where(e.available_at <= ctx.known_at)
        .ext(distinct_on(e.article_id))
        .order_by(e.article_id, e.available_at.desc(), e.id.desc())
        .subquery()
    )
    stmt = (
        select(
            func.count(func.distinct(e.article_id)),
            func.max(e.event_time),
            func.max(e.available_at),
        )
        .join(newest, newest.c.id == e.id)
        .join(rel, rel.event_id == e.id)
        .where(
            rel.asset_id == ctx.asset_id,
            rel.relationship == AssetRelationship.DIRECT_PROVIDER_SYMBOL,
            rel.static_relevance >= p["min_relevance"],
            e.event_time > start,
            e.event_time <= ctx.data_cutoff,
        )
    )
    category = str(p["category"])
    if category != "ANY":
        c = EventClassificationRecord
        stmt = stmt.where(exists().where(and_(c.event_id == e.id, c.category == category)))
    count, latest, available = session.execute(stmt).one()
    coverage_end = max((min(hi, ctx.known_at) for _, hi, _ in intervals), default=None)
    available_at = available or coverage_end
    inputs = {**coverage_inputs, "articles": int(count)}
    if feature.definition.name == "news_event_count":
        return _value(feature, FeatureStatus.VALID, None, float(count), available_at, inputs)
    if latest is None:
        return _value(
            feature,
            FeatureStatus.UNDEFINED,
            "no matching article in the covered window",
            None,
            available_at,
            inputs,
        )
    hours = (ctx.data_cutoff - latest).total_seconds() / 3600
    return _value(feature, FeatureStatus.VALID, None, hours, available_at, inputs)


__all__ = ["StoredContext", "compute_stored_features", "coverage_intervals", "covered"]

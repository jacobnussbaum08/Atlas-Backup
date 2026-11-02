"""Point-in-time reads for asset qualification (Phase 3A).

Every read takes ``as_of`` and returns only what Atlas had at that time:

* reference versions observed at or before ``as_of`` (never today's metadata),
* mappings valid at ``as_of``,
* bars ingested and available at or before ``as_of`` (newest such revision per bar),
* coverage evidence recorded and gaps detected at or before ``as_of``,
* quotes available at or before ``as_of``.

Callers own the transaction; nothing here writes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.asset_discovery.metrics import DailyBar, SpreadSummary
from app.asset_discovery.sessions import ExchangeCalendar, SessionDay, local_date
from app.data_engine.enums import DataType, GapStatus, PriceAdjustment
from app.database.models import (
    AssetIdentifier,
    AssetReferenceVersionRecord,
    CoverageRecord,
    ExchangeRecord,
    MarketBarRecord,
    MarketDataGapRecord,
)

DAILY = "1Day"


def _ids(asset_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    return sorted(set(asset_ids))


def reference_versions_at(
    session: Session, provider: str, as_of: datetime, asset_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, AssetReferenceVersionRecord]:
    stmt = (
        select(AssetReferenceVersionRecord)
        .where(
            AssetReferenceVersionRecord.provider == provider,
            AssetReferenceVersionRecord.observed_at <= as_of,
        )
        .ext(distinct_on(AssetReferenceVersionRecord.asset_id))
        .order_by(AssetReferenceVersionRecord.asset_id, AssetReferenceVersionRecord.version.desc())
    )
    if asset_ids is not None:
        stmt = stmt.where(AssetReferenceVersionRecord.asset_id.in_(_ids(asset_ids)))
    return {v.asset_id: v for v in session.scalars(stmt)}


def mappings_at(
    session: Session, provider: str, as_of: datetime, asset_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, AssetIdentifier]:
    """Mappings valid at ``as_of`` that Atlas had recorded by then."""
    stmt = select(AssetIdentifier).where(
        AssetIdentifier.provider == provider,
        AssetIdentifier.valid_from <= as_of,
        or_(AssetIdentifier.valid_to.is_(None), AssetIdentifier.valid_to > as_of),
        AssetIdentifier.created_at <= as_of,
    )
    if asset_ids is not None:
        stmt = stmt.where(AssetIdentifier.asset_id.in_(_ids(asset_ids)))
    return {m.asset_id: m for m in session.scalars(stmt)}


def exchange_calendars(session: Session) -> dict[str, str | None]:
    return {row.mic: row.calendar_code for row in session.scalars(select(ExchangeRecord))}


def daily_bars(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    adjustment: PriceAdjustment,
    sessions: Sequence[SessionDay],
    calendar: ExchangeCalendar,
    as_of: datetime,
    known_at: datetime | None = None,
) -> list[DailyBar]:
    """Newest revision, as known at ``known_at`` (default ``as_of``), of each session's
    daily bar. ``sessions`` must already be limited to sessions completed by ``as_of``;
    a later ``known_at`` (reconstruction) admits bars retrieved after ``as_of``."""
    known = known_at or as_of
    ids = _ids(asset_ids)
    if not ids or not sessions:
        return []
    start = sessions[0].regular_open - timedelta(days=1)
    end = sessions[-1].regular_close
    b = MarketBarRecord
    stmt = (
        select(
            b.asset_id,
            b.event_time,
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
            b.vwap,
            b.quality_status,
            b.available_at,
        )
        .where(
            b.asset_id.in_(ids),
            b.timeframe == DAILY,
            b.provider == provider,
            b.feed == feed,
            b.adjustment == adjustment,
            b.event_time >= start,
            b.event_time <= end,
            b.ingested_at <= known,
            b.available_at <= known,
        )
        .ext(distinct_on(b.asset_id, b.event_time))
        .order_by(b.asset_id, b.event_time, b.revision.desc())
    )
    wanted = {s.day for s in sessions}
    bars = []
    for row in session.execute(stmt):
        day = local_date(calendar, row.event_time)
        if day not in wanted:
            continue
        bars.append(
            DailyBar(
                asset_id=row.asset_id,
                session=day,
                event_time=row.event_time,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=None if row.volume is None else float(row.volume),
                vwap=None if row.vwap is None else float(row.vwap),
                quality_status=row.quality_status.value,
                available_at=row.available_at,
            )
        )
    return bars


def valid_bar_counts(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    adjustment: PriceAdjustment,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> dict[uuid.UUID, int]:
    """Cheap pre-check: VALID daily bars per asset in [start, end] (any revision)."""
    ids = _ids(asset_ids)
    if not ids:
        return {}
    b = MarketBarRecord
    stmt = (
        select(b.asset_id, func.count(func.distinct(b.event_time)))
        .where(
            b.asset_id.in_(ids),
            b.timeframe == DAILY,
            b.provider == provider,
            b.feed == feed,
            b.adjustment == adjustment,
            b.event_time >= start,
            b.event_time <= end,
            b.ingested_at <= as_of,
            b.quality_status == "VALID",
        )
        .group_by(b.asset_id)
    )
    return {asset_id: int(count) for asset_id, count in session.execute(stmt)}


def _session_starts(
    sessions: Sequence[SessionDay], calendar: ExchangeCalendar
) -> list[tuple[date, datetime]]:
    # Daily bars start at local midnight of the session date.
    return [
        (
            s.day,
            datetime.combine(s.day, datetime.min.time(), tzinfo=calendar.zone).astimezone(
                s.regular_open.tzinfo
            ),
        )
        for s in sessions
    ]


def covered_sessions(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    adjustment: PriceAdjustment,
    sessions: Sequence[SessionDay],
    calendar: ExchangeCalendar,
    as_of: datetime,
) -> dict[uuid.UUID, set[date]]:
    """Sessions whose daily bar lies inside recorded completeness evidence."""
    ids = _ids(asset_ids)
    if not ids or not sessions:
        return {}
    starts = _session_starts(sessions, calendar)
    c = CoverageRecord
    rows = session.execute(
        select(c.asset_id, c.covered_start, c.covered_end).where(
            c.asset_id.in_(ids),
            c.provider == provider,
            c.feed == feed,
            c.data_type == DataType.BAR,
            c.timeframe == DAILY,
            c.adjustment == adjustment,
            c.recorded_at <= as_of,
            c.covered_end > starts[0][1],
            c.covered_start <= starts[-1][1],
        )
    )
    covered: dict[uuid.UUID, set[date]] = defaultdict(set)
    for asset_id, lo, hi in rows:
        covered[asset_id].update(day for day, at in starts if lo <= at < hi)
    return dict(covered)


def gap_sessions(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    sessions: Sequence[SessionDay],
    calendar: ExchangeCalendar,
    as_of: datetime,
) -> dict[uuid.UUID, set[date]]:
    """Sessions inside a daily-bar gap that was detected and still unresolved at ``as_of``."""
    ids = _ids(asset_ids)
    if not ids or not sessions:
        return {}
    starts = _session_starts(sessions, calendar)
    g = MarketDataGapRecord
    rows = session.execute(
        select(g.asset_id, g.gap_start, g.gap_end).where(
            g.asset_id.in_(ids),
            g.provider == provider,
            g.feed == feed,
            g.data_type == DataType.BAR,
            g.timeframe == DAILY,
            g.detected_at <= as_of,
            or_(
                g.status.in_([GapStatus.OPEN, GapStatus.RECOVERING, GapStatus.UNAVAILABLE]),
                and_(g.resolved_at.is_not(None), g.resolved_at > as_of),
            ),
            g.gap_end > starts[0][1],
            g.gap_start <= starts[-1][1],
        )
    )
    gaps: dict[uuid.UUID, set[date]] = defaultdict(set)
    for asset_id, lo, hi in rows:
        gaps[asset_id].update(day for day, at in starts if lo <= at < hi)
    return dict(gaps)


_SPREAD_SQL = """
SELECT asset_id,
       count(*) AS quotes,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 10000
       ) FILTER (WHERE quality_status = 'VALID') AS median_spread_bps,
       avg(CASE WHEN quality_status = 'QUARANTINED' THEN 1.0 ELSE 0.0 END) AS invalid_ratio
FROM market_quotes
WHERE asset_id = ANY(:ids) AND provider = :provider AND feed = :feed
  AND available_at <= :as_of
  AND bid_price > 0 AND ask_price > 0
  AND ({windows})
GROUP BY asset_id
"""


def spread_summaries(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    sessions: Sequence[SessionDay],
    as_of: datetime,
) -> dict[uuid.UUID, SpreadSummary]:
    """Quoted spreads during the regular sessions of ``sessions`` (two-sided quotes)."""
    ids = _ids(asset_ids)
    if not ids or not sessions:
        return {}
    params: dict[str, object] = {"ids": ids, "provider": provider, "feed": feed, "as_of": as_of}
    clauses = []
    for index, day in enumerate(sessions):
        params[f"o{index}"], params[f"c{index}"] = day.regular_open, day.regular_close
        clauses.append(f"(event_time >= :o{index} AND event_time < :c{index})")
    sql = text(_SPREAD_SQL.format(windows=" OR ".join(clauses)))
    result = {}
    for row in session.execute(sql, params):
        result[row.asset_id] = SpreadSummary(
            quotes=int(row.quotes),
            median_spread_bps=None
            if row.median_spread_bps is None
            else float(row.median_spread_bps),
            invalid_ratio=None if row.invalid_ratio is None else float(row.invalid_ratio),
            window_start=sessions[0].regular_open,
            window_end=min(sessions[-1].regular_close, as_of),
        )
    return result


__all__ = [
    "covered_sessions",
    "daily_bars",
    "exchange_calendars",
    "gap_sessions",
    "mappings_at",
    "reference_versions_at",
    "spread_summaries",
    "valid_bar_counts",
]

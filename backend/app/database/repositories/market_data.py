"""Persistence for historical market data: bars, raw events, quality events, jobs.

Bars are append-only. A *series* is (asset, timeframe, provider, feed, adjustment);
writers to one series are serialized with a transaction-scoped advisory lock, so
two overlapping ingestion jobs can never race to the same revision number. The
current view of a bar is its highest revision; ``known_at`` restricts the view to
what Atlas had stored at a past time (point-in-time reads).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app.data_engine.enums import (
    DataQualityStatus,
    IngestionJobStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
)
from app.database.models import (
    DataQualityEventRecord,
    IngestionJobRecord,
    IngestionJobSymbolRecord,
    MarketBarRecord,
    RawMarketDataEventRecord,
)

COUNTER_FIELDS = (
    "pages_retrieved",
    "records_retrieved",
    "records_validated",
    "records_stored",
    "records_duplicate",
    "records_revised",
    "records_rejected",
    "records_quarantined",
)


@dataclass(frozen=True)
class SeriesKey:
    asset_id: uuid.UUID
    timeframe: str
    provider: str
    feed: str
    adjustment: PriceAdjustment

    def lock_name(self) -> str:
        return (
            f"market_bars:{self.asset_id}:{self.timeframe}:{self.provider}:"
            f"{self.feed}:{self.adjustment.value}"
        )


@dataclass(frozen=True)
class CoverageRow:
    timeframe: str
    provider: str
    feed: str
    adjustment: str
    first_event_at: datetime
    last_event_at: datetime
    bars: int
    quarantined: int
    incomplete: int


class MarketBarRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_series(self, key: SeriesKey) -> None:
        """Serialize writers of one series until the transaction ends."""
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key.lock_name(), 0)))
        )

    def latest_revisions(
        self, key: SeriesKey, event_times: Iterable[datetime]
    ) -> dict[datetime, MarketBarRecord]:
        times = sorted(set(event_times))
        if not times:
            return {}
        stmt = (
            select(MarketBarRecord)
            .where(*self._series(key), MarketBarRecord.event_time.in_(times))
            .order_by(MarketBarRecord.event_time, MarketBarRecord.revision.desc())
            .ext(distinct_on(MarketBarRecord.event_time))
        )
        return {row.event_time: row for row in self._session.scalars(stmt)}

    def insert(self, rows: Sequence[dict[str, Any]]) -> int:
        if rows:
            self._session.execute(insert(MarketBarRecord), list(rows))
        return len(rows)

    def current_bars(
        self,
        key: SeriesKey,
        *,
        start: datetime,
        end: datetime,
        statuses: Iterable[DataQualityStatus],
        known_at: datetime | None = None,
        after: datetime | None = None,
        limit: int = 1000,
    ) -> Sequence[MarketBarRecord]:
        """Latest revision per bar in [start, end], then filtered by status.

        Filtering happens *after* choosing the latest revision, so a quarantined
        correction hides the bar instead of resurrecting an older revision.
        """
        latest = (
            select(MarketBarRecord.id)
            .where(
                *self._series(key),
                MarketBarRecord.event_time >= start,
                MarketBarRecord.event_time <= end,
            )
            .order_by(MarketBarRecord.event_time, MarketBarRecord.revision.desc())
            .ext(distinct_on(MarketBarRecord.event_time))
        )
        if known_at is not None:
            latest = latest.where(MarketBarRecord.ingested_at <= known_at)
        if after is not None:
            latest = latest.where(MarketBarRecord.event_time > after)
        ids = latest.subquery()
        stmt = (
            select(MarketBarRecord)
            .where(
                MarketBarRecord.id.in_(select(ids.c.id)),
                MarketBarRecord.quality_status.in_(list(statuses)),
            )
            .order_by(MarketBarRecord.event_time)
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def revisions(self, key: SeriesKey, event_time: datetime) -> Sequence[MarketBarRecord]:
        stmt = (
            select(MarketBarRecord)
            .where(*self._series(key), MarketBarRecord.event_time == event_time)
            .order_by(MarketBarRecord.revision)
        )
        return self._session.scalars(stmt).all()

    def coverage(self, asset_id: uuid.UUID) -> list[CoverageRow]:
        """Stored ranges per series, counted over the current revision of each bar."""
        bar = aliased(MarketBarRecord)
        latest = (
            select(bar)
            .where(bar.asset_id == asset_id)
            .order_by(
                bar.timeframe,
                bar.provider,
                bar.feed,
                bar.adjustment,
                bar.event_time,
                bar.revision.desc(),
            )
            .ext(distinct_on(bar.timeframe, bar.provider, bar.feed, bar.adjustment, bar.event_time))
            .subquery()
        )
        status = latest.c.quality_status
        stmt = (
            select(
                latest.c.timeframe,
                latest.c.provider,
                latest.c.feed,
                latest.c.adjustment,
                func.min(latest.c.event_time),
                func.max(latest.c.event_time),
                func.count(),
                func.count().filter(status == DataQualityStatus.QUARANTINED.value),
                func.count().filter(status == DataQualityStatus.INCOMPLETE.value),
            )
            .group_by(latest.c.timeframe, latest.c.provider, latest.c.feed, latest.c.adjustment)
            .order_by(latest.c.timeframe, latest.c.feed)
        )
        return [CoverageRow(*row) for row in self._session.execute(stmt)]

    @staticmethod
    def _series(key: SeriesKey) -> list[Any]:
        return [
            MarketBarRecord.asset_id == key.asset_id,
            MarketBarRecord.timeframe == key.timeframe,
            MarketBarRecord.provider == key.provider,
            MarketBarRecord.feed == key.feed,
            MarketBarRecord.adjustment == key.adjustment,
        ]


class MarketDataEventRepository:
    """Raw provider messages and data-quality events (both append-only)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_raw(self, record: RawMarketDataEventRecord) -> int:
        self._session.add(record)
        self._session.flush()
        return record.id

    def add_quality_events(self, records: Sequence[DataQualityEventRecord]) -> None:
        if records:
            self._session.add_all(list(records))
            self._session.flush()

    def quality_events(
        self,
        *,
        job_id: uuid.UUID | None = None,
        asset_id: uuid.UUID | None = None,
        event_types: Iterable[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[DataQualityEventRecord]:
        stmt = select(DataQualityEventRecord)
        if job_id is not None:
            stmt = stmt.where(DataQualityEventRecord.ingestion_job_id == job_id)
        if asset_id is not None:
            stmt = stmt.where(DataQualityEventRecord.asset_id == asset_id)
        if event_types:
            stmt = stmt.where(DataQualityEventRecord.event_type.in_(list(event_types)))
        stmt = stmt.order_by(DataQualityEventRecord.id.desc()).limit(limit).offset(offset)
        return self._session.scalars(stmt).all()


class IngestionJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, job: IngestionJobRecord, symbols: Sequence[tuple[str, uuid.UUID]]
    ) -> IngestionJobRecord:
        self._session.add(job)
        self._session.flush()
        self._session.add_all(
            IngestionJobSymbolRecord(job_id=job.id, provider_symbol=symbol, asset_id=asset_id)
            for symbol, asset_id in symbols
        )
        self._session.flush()
        return job

    def get(self, job_id: uuid.UUID, *, lock: bool = False) -> IngestionJobRecord | None:
        return self._session.get(IngestionJobRecord, job_id, with_for_update=lock)

    def symbols(self, job_id: uuid.UUID) -> Sequence[IngestionJobSymbolRecord]:
        stmt = (
            select(IngestionJobSymbolRecord)
            .where(IngestionJobSymbolRecord.job_id == job_id)
            .order_by(IngestionJobSymbolRecord.provider_symbol)
        )
        return self._session.scalars(stmt).all()

    def list(
        self, *, statuses: Iterable[IngestionJobStatus] | None = None, limit: int, offset: int
    ) -> Sequence[IngestionJobRecord]:
        stmt = select(IngestionJobRecord)
        if statuses:
            stmt = stmt.where(IngestionJobRecord.status.in_(list(statuses)))
        stmt = stmt.order_by(IngestionJobRecord.created_at.desc()).limit(limit).offset(offset)
        return self._session.scalars(stmt).all()

    def add_counts(
        self, job_id: uuid.UUID, provider_symbol: str, counts: dict[str, int], at: datetime
    ) -> None:
        """Increment counters on the symbol row and the job row (same transaction)."""
        increments = {name: counts.get(name, 0) for name in COUNTER_FIELDS}
        symbol_table = IngestionJobSymbolRecord
        self._session.execute(
            update(symbol_table)
            .where(
                and_(symbol_table.job_id == job_id, symbol_table.provider_symbol == provider_symbol)
            )
            .values(
                {
                    getattr(symbol_table, k): getattr(symbol_table, k) + v
                    for k, v in increments.items()
                }
            )
        )
        self._session.execute(
            update(IngestionJobRecord)
            .where(IngestionJobRecord.id == job_id)
            .values(
                {
                    **{
                        getattr(IngestionJobRecord, k): getattr(IngestionJobRecord, k) + v
                        for k, v in increments.items()
                    },
                    IngestionJobRecord.heartbeat_at: at,
                }
            )
        )

    def extend_event_range(
        self, job_id: uuid.UUID, provider_symbol: str, first: datetime, last: datetime
    ) -> None:
        row = self._session.get(IngestionJobSymbolRecord, (job_id, provider_symbol))
        if row is None:
            return
        row.first_event_at = first if row.first_event_at is None else min(row.first_event_at, first)
        row.last_event_at = last if row.last_event_at is None else max(row.last_event_at, last)

    def set_symbol_status(
        self,
        job_id: uuid.UUID,
        provider_symbol: str,
        status: IngestionSymbolStatus,
        error: str | None = None,
    ) -> None:
        self._session.execute(
            update(IngestionJobSymbolRecord)
            .where(
                IngestionJobSymbolRecord.job_id == job_id,
                IngestionJobSymbolRecord.provider_symbol == provider_symbol,
            )
            .values(status=status, error=error)
        )

    def mark_interrupted(self, *, reason: str, at: datetime) -> int:
        """Fail every job left PENDING/RUNNING by a process that no longer exists."""
        unfinished = (IngestionJobStatus.PENDING, IngestionJobStatus.RUNNING)
        job_ids = list(
            self._session.scalars(
                select(IngestionJobRecord.id).where(IngestionJobRecord.status.in_(unfinished))
            )
        )
        if not job_ids:
            return 0
        self._session.execute(
            update(IngestionJobSymbolRecord)
            .where(
                IngestionJobSymbolRecord.job_id.in_(job_ids),
                IngestionJobSymbolRecord.status.in_(
                    (IngestionSymbolStatus.PENDING, IngestionSymbolStatus.RUNNING)
                ),
            )
            .values(status=IngestionSymbolStatus.FAILED, error=reason)
        )
        self._session.execute(
            update(IngestionJobRecord)
            .where(IngestionJobRecord.id.in_(job_ids))
            .values(status=IngestionJobStatus.FAILED, error=reason, completed_at=at)
        )
        return len(job_ids)

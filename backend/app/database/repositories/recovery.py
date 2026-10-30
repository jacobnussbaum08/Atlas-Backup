"""Persistence for completeness evidence, gaps and recovery jobs (Deliverable 2D).

Concurrency: writers of one series take a transaction-scoped advisory lock
(:meth:`CompletenessRepository.lock_series`) before reading and changing its gaps, so
overlapping detections and recoveries are serialized. The database backs this with an
exclusion constraint (unresolved gaps of a series never overlap) and a partial unique
index (at most one active recovery job per gap). Jobs are claimed with
``FOR UPDATE SKIP LOCKED`` and a lease, so several workers never run the same job and a
job whose worker died is picked up again once its lease expires.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data_engine.completeness import Interval, subtract
from app.data_engine.enums import (
    UNRESOLVED_GAP_STATUSES,
    CoverageEvidence,
    DataType,
    GapCause,
    GapStatus,
    PriceAdjustment,
    RecoveryJobKind,
    RecoveryJobStatus,
)
from app.database.models import (
    CoverageRecord,
    MarketDataGapRecord,
    RecoveryJobRecord,
    RecoveryStatusRecord,
)

ACTIVE_JOB_STATUSES = (RecoveryJobStatus.PENDING, RecoveryJobStatus.RUNNING)


@dataclass(frozen=True)
class Series:
    """One completeness-tracked series. Trades and quotes have no timeframe/adjustment."""

    asset_id: uuid.UUID
    provider: str
    feed: str
    data_type: DataType
    timeframe: str | None = None
    adjustment: PriceAdjustment | None = None

    def lock_name(self) -> str:
        return (
            f"completeness:{self.asset_id}:{self.provider}:{self.feed}:{self.data_type.value}:"
            f"{self.timeframe or '-'}"
        )

    def describe(self) -> str:
        suffix = f" {self.timeframe}" if self.timeframe else ""
        return f"{self.data_type.value.lower()}{suffix}"


def _series_filter(model: Any, series: Series) -> list[Any]:
    clauses = [
        model.asset_id == series.asset_id,
        model.provider == series.provider,
        model.feed == series.feed,
        model.data_type == series.data_type,
    ]
    if series.timeframe is None:
        clauses.append(model.timeframe.is_(None))
    else:
        clauses.append(model.timeframe == series.timeframe)
    return clauses


class CompletenessRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_series(self, series: Series) -> None:
        """Serialize completeness writers of one series until the transaction ends."""
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(series.lock_name(), 0)))
        )

    # ------------------------------------------------------------ coverage
    def add_coverage(
        self,
        series: Series,
        interval: Interval,
        evidence: CoverageEvidence,
        *,
        at: datetime,
        ingestion_job_id: uuid.UUID | None = None,
        stream_session_id: uuid.UUID | None = None,
    ) -> int:
        record = CoverageRecord(
            asset_id=series.asset_id,
            provider=series.provider,
            feed=series.feed,
            data_type=series.data_type,
            timeframe=series.timeframe,
            adjustment=series.adjustment,
            covered_start=interval[0],
            covered_end=interval[1],
            evidence=evidence,
            ingestion_job_id=ingestion_job_id,
            stream_session_id=stream_session_id,
            recorded_at=at,
        )
        self._session.add(record)
        self._session.flush()
        return record.id

    def extend_coverage(self, coverage_id: int, end: datetime) -> None:
        """Extend a live segment (never shortens it)."""
        self._session.execute(
            update(CoverageRecord)
            .where(CoverageRecord.id == coverage_id, CoverageRecord.covered_end < end)
            .values(covered_end=end)
        )

    def covered(
        self,
        series: Series,
        bounds: Interval,
        evidence: Sequence[CoverageEvidence] = tuple(CoverageEvidence),
    ) -> list[Interval]:
        stmt = select(CoverageRecord.covered_start, CoverageRecord.covered_end).where(
            *_series_filter(CoverageRecord, series),
            CoverageRecord.evidence.in_(list(evidence)),
            CoverageRecord.covered_start < bounds[1],
            CoverageRecord.covered_end > bounds[0],
        )
        return [(start, end) for start, end in self._session.execute(stmt)]

    def jobs_with_coverage(self, job_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not job_ids:
            return set()
        stmt = select(CoverageRecord.ingestion_job_id).where(
            CoverageRecord.ingestion_job_id.in_(list(job_ids))
        )
        return {job_id for job_id in self._session.scalars(stmt) if job_id is not None}

    # ------------------------------------------------------------ gaps
    def unresolved_gaps(self, series: Series, bounds: Interval) -> Sequence[MarketDataGapRecord]:
        stmt = (
            select(MarketDataGapRecord)
            .where(
                *_series_filter(MarketDataGapRecord, series),
                MarketDataGapRecord.status.in_(list(UNRESOLVED_GAP_STATUSES)),
                MarketDataGapRecord.gap_start < bounds[1],
                MarketDataGapRecord.gap_end > bounds[0],
            )
            .order_by(MarketDataGapRecord.gap_start)
        )
        return self._session.scalars(stmt).all()

    def record_gaps(
        self,
        series: Series,
        provider_symbol: str,
        intervals: Sequence[Interval],
        cause: GapCause,
        *,
        at: datetime,
        stream_gap_id: int | None = None,
        parent_gap_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> list[MarketDataGapRecord]:
        """Record missing intervals. Parts already covered by an unresolved gap of the series
        are not recorded again (the caller must hold the series lock)."""
        created = []
        for start, end in intervals:
            existing = [
                (g.gap_start, g.gap_end) for g in self.unresolved_gaps(series, (start, end))
            ]
            for part_start, part_end in subtract([(start, end)], existing):
                record = MarketDataGapRecord(
                    asset_id=series.asset_id,
                    provider=series.provider,
                    feed=series.feed,
                    provider_symbol=provider_symbol,
                    data_type=series.data_type,
                    timeframe=series.timeframe,
                    adjustment=series.adjustment,
                    gap_start=part_start,
                    gap_end=part_end,
                    cause=cause,
                    status=GapStatus.OPEN,
                    stream_gap_id=stream_gap_id,
                    parent_gap_id=parent_gap_id,
                    detected_at=at,
                    details=details or {},
                )
                self._session.add(record)
                created.append(record)
        self._session.flush()
        return created

    def get_gap(self, gap_id: int, *, lock: bool = False) -> MarketDataGapRecord | None:
        return self._session.get(MarketDataGapRecord, gap_id, with_for_update=lock)

    def gaps(
        self,
        *,
        provider: str,
        feed: str,
        asset_id: uuid.UUID | None = None,
        data_type: DataType | None = None,
        statuses: Sequence[GapStatus] = (),
        stream_gap_id: int | None = None,
        limit: int = 100,
        after_id: int | None = None,
    ) -> Sequence[MarketDataGapRecord]:
        stmt = select(MarketDataGapRecord).where(
            MarketDataGapRecord.provider == provider, MarketDataGapRecord.feed == feed
        )
        if asset_id is not None:
            stmt = stmt.where(MarketDataGapRecord.asset_id == asset_id)
        if data_type is not None:
            stmt = stmt.where(MarketDataGapRecord.data_type == data_type)
        if statuses:
            stmt = stmt.where(MarketDataGapRecord.status.in_(list(statuses)))
        if stream_gap_id is not None:
            stmt = stmt.where(MarketDataGapRecord.stream_gap_id == stream_gap_id)
        if after_id is not None:
            stmt = stmt.where(MarketDataGapRecord.id < after_id)
        return self._session.scalars(
            stmt.order_by(MarketDataGapRecord.id.desc()).limit(limit)
        ).all()

    def gap_counts(self, provider: str, feed: str) -> dict[str, dict[str, int]]:
        stmt = (
            select(MarketDataGapRecord.data_type, MarketDataGapRecord.status, func.count())
            .where(MarketDataGapRecord.provider == provider, MarketDataGapRecord.feed == feed)
            .group_by(MarketDataGapRecord.data_type, MarketDataGapRecord.status)
        )
        counts: dict[str, dict[str, int]] = {}
        for data_type, status, count in self._session.execute(stmt):
            counts.setdefault(data_type.value, {})[status.value] = count
        return counts


class RecoveryJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, record: RecoveryJobRecord) -> RecoveryJobRecord | None:
        """Insert unless the gap already has an active job (then None)."""
        values = {
            column.key: getattr(record, column.key)
            for column in RecoveryJobRecord.__table__.columns
            if getattr(record, column.key, None) is not None
        }
        stmt = (
            pg_insert(RecoveryJobRecord)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["gap_id"],
                index_where=RecoveryJobRecord.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
            )
            .returning(RecoveryJobRecord.id)
        )
        job_id = self._session.execute(stmt).scalar_one_or_none()
        return None if job_id is None else self._session.get(RecoveryJobRecord, job_id)

    def get(self, job_id: uuid.UUID, *, lock: bool = False) -> RecoveryJobRecord | None:
        return self._session.get(RecoveryJobRecord, job_id, with_for_update=lock)

    def for_gap(self, gap_id: int) -> Sequence[RecoveryJobRecord]:
        stmt = (
            select(RecoveryJobRecord)
            .where(RecoveryJobRecord.gap_id == gap_id)
            .order_by(RecoveryJobRecord.created_at)
        )
        return self._session.scalars(stmt).all()

    def active_reconciliations(
        self, series: Series, bounds: Interval
    ) -> Sequence[RecoveryJobRecord]:
        stmt = select(RecoveryJobRecord).where(
            *_series_filter(RecoveryJobRecord, series),
            RecoveryJobRecord.kind == RecoveryJobKind.RECONCILIATION,
            RecoveryJobRecord.status.in_(list(ACTIVE_JOB_STATUSES)),
            RecoveryJobRecord.missing_start < bounds[1],
            RecoveryJobRecord.missing_end > bounds[0],
        )
        return self._session.scalars(stmt).all()

    def claim_due(
        self,
        *,
        provider: str,
        feed: str,
        now: datetime,
        owner: str,
        lease_until: datetime,
        limit: int,
    ) -> list[uuid.UUID]:
        """Lease due jobs: PENDING ones whose time has come, and RUNNING ones whose lease
        expired (their worker stopped mid-attempt)."""
        stmt = (
            select(RecoveryJobRecord)
            .where(
                RecoveryJobRecord.provider == provider,
                RecoveryJobRecord.feed == feed,
                or_(
                    and_(
                        RecoveryJobRecord.status == RecoveryJobStatus.PENDING,
                        RecoveryJobRecord.next_attempt_at <= now,
                    ),
                    and_(
                        RecoveryJobRecord.status == RecoveryJobStatus.RUNNING,
                        RecoveryJobRecord.lease_expires_at < now,
                    ),
                ),
            )
            .order_by(RecoveryJobRecord.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = []
        for job in self._session.scalars(stmt):
            # RUNNING in the claiming transaction: a concurrent claimer skips the locked row
            # now and no longer sees it as PENDING afterwards.
            job.status = RecoveryJobStatus.RUNNING
            job.attempts += 1
            job.started_at = job.started_at or now
            job.lease_owner = owner
            job.lease_expires_at = lease_until
            claimed.append(job.id)
        self._session.flush()
        return claimed

    def release_leases(self, *, provider: str, feed: str, reason: str) -> int:
        """Return RUNNING jobs to PENDING (their worker is gone). Called by the single
        recovery worker at start-up, while it holds the provider/feed lock."""
        result = self._session.execute(
            update(RecoveryJobRecord)
            .where(
                RecoveryJobRecord.provider == provider,
                RecoveryJobRecord.feed == feed,
                RecoveryJobRecord.status == RecoveryJobStatus.RUNNING,
            )
            .values(
                status=RecoveryJobStatus.PENDING,
                lease_owner=None,
                lease_expires_at=None,
                error=reason,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def jobs(
        self,
        *,
        provider: str,
        feed: str,
        statuses: Sequence[RecoveryJobStatus] = (),
        gap_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[RecoveryJobRecord]:
        stmt = select(RecoveryJobRecord).where(
            RecoveryJobRecord.provider == provider, RecoveryJobRecord.feed == feed
        )
        if statuses:
            stmt = stmt.where(RecoveryJobRecord.status.in_(list(statuses)))
        if gap_id is not None:
            stmt = stmt.where(RecoveryJobRecord.gap_id == gap_id)
        return self._session.scalars(
            stmt.order_by(RecoveryJobRecord.created_at.desc()).limit(limit)
        ).all()

    def job_counts(self, provider: str, feed: str) -> dict[str, int]:
        stmt = (
            select(RecoveryJobRecord.status, func.count())
            .where(RecoveryJobRecord.provider == provider, RecoveryJobRecord.feed == feed)
            .group_by(RecoveryJobRecord.status)
        )
        return {status.value: count for status, count in self._session.execute(stmt)}

    def last_completed_at(self, provider: str, feed: str) -> datetime | None:
        stmt = select(func.max(RecoveryJobRecord.completed_at)).where(
            RecoveryJobRecord.provider == provider,
            RecoveryJobRecord.feed == feed,
            RecoveryJobRecord.status.in_(
                [RecoveryJobStatus.COMPLETED, RecoveryJobStatus.PARTIALLY_COMPLETED]
            ),
        )
        return self._session.execute(stmt).scalar_one_or_none()


class RecoveryStatusRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, provider: str, feed: str) -> RecoveryStatusRecord | None:
        return self._session.get(RecoveryStatusRecord, (provider, feed))

    def upsert(self, provider: str, feed: str, values: dict[str, Any]) -> None:
        stmt = pg_insert(RecoveryStatusRecord).values(provider=provider, feed=feed, **values)
        self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["provider", "feed"], set_={**values, "updated_at": func.now()}
            )
        )


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "CompletenessRepository",
    "RecoveryJobRepository",
    "RecoveryStatusRepository",
    "Series",
]

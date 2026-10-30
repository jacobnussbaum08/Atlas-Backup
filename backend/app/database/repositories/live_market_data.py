"""Persistence for live streaming: quotes, trades, stream sessions, status and gaps.

Quotes and trades are append-only. Their identity is (asset, provider, feed,
event_time_ns, content_sha256), so inserting a message that was already stored is a
no-op that is counted as a duplicate (``ON CONFLICT DO NOTHING``). Live bars use
``MarketBarRepository`` (same table and revision rules as historical bars).

Sessions, status and gaps are operational records about the stream itself; they are
updated in place as the worker's state changes. Market observations never are.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.data_engine.enums import GapRecoveryStatus, StreamGapKind
from app.database.models import (
    MarketQuoteRecord,
    MarketTradeRecord,
    StreamGapRecord,
    StreamSessionRecord,
    StreamStatusRecord,
)


class LiveObservationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def insert_quotes(self, rows: Sequence[dict[str, Any]]) -> list[int]:
        """Insert quotes; returns the indexes of ``rows`` that were new (the rest were
        duplicates of stored observations)."""
        return self._insert(MarketQuoteRecord, "uq_market_quotes_observation", rows)

    def insert_trades(self, rows: Sequence[dict[str, Any]]) -> list[int]:
        return self._insert(MarketTradeRecord, "uq_market_trades_observation", rows)

    def _insert(
        self,
        model: type[MarketQuoteRecord] | type[MarketTradeRecord],
        constraint: str,
        rows: Sequence[dict[str, Any]],
    ) -> list[int]:
        if not rows:
            return []
        # PostgreSQL accepts at most 65535 bind parameters per statement.
        chunk = max(1, 60_000 // len(rows[0]))
        inserted: set[tuple[Any, ...]] = set()
        for offset in range(0, len(rows), chunk):
            stmt = (
                pg_insert(model)
                .values(list(rows[offset : offset + chunk]))
                .on_conflict_do_nothing(constraint=constraint)
                .returning(model.asset_id, model.event_time_ns, model.content_sha256)
            )
            inserted.update(tuple(row) for row in self._session.execute(stmt))
        new: list[int] = []
        for index, row in enumerate(rows):
            key = (row["asset_id"], row["event_time_ns"], row["content_sha256"])
            if key in inserted:
                inserted.discard(key)  # a repeat within ``rows`` is a duplicate too
                new.append(index)
        return new

    def quotes(
        self,
        asset_id: uuid.UUID,
        feed: str,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        after_id: int | None = None,
    ) -> Sequence[MarketQuoteRecord]:
        stmt = select(MarketQuoteRecord).where(
            MarketQuoteRecord.asset_id == asset_id,
            MarketQuoteRecord.feed == feed,
            MarketQuoteRecord.event_time >= start,
            MarketQuoteRecord.event_time <= end,
        )
        if after_id is not None:
            stmt = stmt.where(MarketQuoteRecord.id > after_id)
        stmt = stmt.order_by(MarketQuoteRecord.id).limit(limit)  # storage (arrival) order
        return self._session.scalars(stmt).all()

    def trades(
        self,
        asset_id: uuid.UUID,
        feed: str,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        after_id: int | None = None,
    ) -> Sequence[MarketTradeRecord]:
        stmt = select(MarketTradeRecord).where(
            MarketTradeRecord.asset_id == asset_id,
            MarketTradeRecord.feed == feed,
            MarketTradeRecord.event_time >= start,
            MarketTradeRecord.event_time <= end,
        )
        if after_id is not None:
            stmt = stmt.where(MarketTradeRecord.id > after_id)
        stmt = stmt.order_by(MarketTradeRecord.id).limit(limit)  # storage (arrival) order
        return self._session.scalars(stmt).all()


class StreamRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------ sessions
    def create_session(self, record: StreamSessionRecord) -> StreamSessionRecord:
        self._session.add(record)
        self._session.flush()
        return record

    def update_session(self, session_id: uuid.UUID, values: dict[str, Any]) -> None:
        if values:
            self._session.execute(
                update(StreamSessionRecord)
                .where(StreamSessionRecord.id == session_id)
                .values(values)
            )

    def latest_session(self, provider: str, feed: str) -> StreamSessionRecord | None:
        return self._session.scalars(
            select(StreamSessionRecord)
            .where(StreamSessionRecord.provider == provider, StreamSessionRecord.feed == feed)
            .order_by(StreamSessionRecord.started_at.desc())
            .limit(1)
        ).first()

    def close_unfinished_sessions(
        self, provider: str, feed: str, *, at: datetime, reason: str
    ) -> list[StreamSessionRecord]:
        """End sessions left open by a worker process that no longer exists."""
        open_sessions = list(
            self._session.scalars(
                select(StreamSessionRecord).where(
                    StreamSessionRecord.provider == provider,
                    StreamSessionRecord.feed == feed,
                    StreamSessionRecord.ended_at.is_(None),
                )
            )
        )
        for record in open_sessions:
            record.ended_at = max(at, record.started_at)
            record.end_reason = reason
            record.state = "FAILED"
        self._session.flush()
        return open_sessions

    def sessions(
        self, provider: str, feed: str, *, limit: int, offset: int = 0
    ) -> Sequence[StreamSessionRecord]:
        return self._session.scalars(
            select(StreamSessionRecord)
            .where(StreamSessionRecord.provider == provider, StreamSessionRecord.feed == feed)
            .order_by(StreamSessionRecord.started_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()

    # ------------------------------------------------------------ status
    def get_status(self, provider: str, feed: str) -> StreamStatusRecord | None:
        return self._session.get(StreamStatusRecord, (provider, feed))

    def upsert_status(self, provider: str, feed: str, values: dict[str, Any]) -> None:
        row = {"provider": provider, "feed": feed, **values}
        stmt = pg_insert(StreamStatusRecord).values(row)
        changes: dict[str, Any] = {k: stmt.excluded[k] for k in values}
        changes["updated_at"] = func.now()
        self._session.execute(
            stmt.on_conflict_do_update(index_elements=["provider", "feed"], set_=changes)
        )

    # ------------------------------------------------------------ gaps
    def open_gap(
        self,
        *,
        provider: str,
        feed: str,
        kind: StreamGapKind,
        gap_start: datetime,
        reason: str,
        symbols: Sequence[str],
        channels: Sequence[str],
        detected_at: datetime,
        previous_session_id: uuid.UUID | None,
        last_message_before: datetime | None,
        frames_dropped: int = 0,
        gap_end: datetime | None = None,
    ) -> StreamGapRecord:
        record = StreamGapRecord(
            provider=provider,
            feed=feed,
            kind=kind,
            gap_start=gap_start,
            gap_end=gap_end,
            reason=reason,
            symbols=list(symbols),
            channels=list(channels),
            detected_at=detected_at,
            closed_at=detected_at if gap_end is not None else None,
            previous_session_id=previous_session_id,
            last_message_before=last_message_before,
            frames_dropped=frames_dropped,
            recovery_status=GapRecoveryStatus.PENDING,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def close_open_gaps(
        self, provider: str, feed: str, *, at: datetime, recovery_session_id: uuid.UUID | None
    ) -> int:
        result = self._session.execute(
            update(StreamGapRecord)
            .where(
                StreamGapRecord.provider == provider,
                StreamGapRecord.feed == feed,
                StreamGapRecord.gap_end.is_(None),
            )
            .values(
                gap_end=func.greatest(StreamGapRecord.gap_start, at),
                closed_at=at,
                recovery_session_id=recovery_session_id,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def gaps(
        self,
        provider: str,
        feed: str,
        *,
        open_only: bool = False,
        limit: int,
        offset: int = 0,
    ) -> Sequence[StreamGapRecord]:
        stmt = select(StreamGapRecord).where(
            StreamGapRecord.provider == provider, StreamGapRecord.feed == feed
        )
        if open_only:
            stmt = stmt.where(StreamGapRecord.gap_end.is_(None))
        return self._session.scalars(
            stmt.order_by(StreamGapRecord.gap_start.desc()).limit(limit).offset(offset)
        ).all()


__all__ = ["LiveObservationRepository", "StreamRepository"]

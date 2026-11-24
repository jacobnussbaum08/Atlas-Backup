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

from app.data_engine.enums import DataQualityStatus, GapRecoveryStatus, StreamGapKind
from app.database.models import (
    MarketBarRecord,
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
        newest_first: bool = False,
    ) -> Sequence[MarketQuoteRecord]:
        stmt = select(MarketQuoteRecord).where(
            MarketQuoteRecord.asset_id == asset_id,
            MarketQuoteRecord.feed == feed,
            MarketQuoteRecord.event_time >= start,
            MarketQuoteRecord.event_time <= end,
        )
        if after_id is not None:
            stmt = stmt.where(MarketQuoteRecord.id > after_id)
        # Storage (arrival) order; newest_first returns the latest ``limit`` stored rows.
        order = MarketQuoteRecord.id.desc() if newest_first else MarketQuoteRecord.id
        stmt = stmt.order_by(order).limit(limit)
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
        newest_first: bool = False,
    ) -> Sequence[MarketTradeRecord]:
        stmt = select(MarketTradeRecord).where(
            MarketTradeRecord.asset_id == asset_id,
            MarketTradeRecord.feed == feed,
            MarketTradeRecord.event_time >= start,
            MarketTradeRecord.event_time <= end,
        )
        if after_id is not None:
            stmt = stmt.where(MarketTradeRecord.id > after_id)
        # Storage (arrival) order; newest_first returns the latest ``limit`` stored rows.
        order = MarketTradeRecord.id.desc() if newest_first else MarketTradeRecord.id
        stmt = stmt.order_by(order).limit(limit)
        return self._session.scalars(stmt).all()

    def latest_quote(
        self, asset_id: uuid.UUID, feed: str, statuses: Sequence[DataQualityStatus]
    ) -> MarketQuoteRecord | None:
        """Quote with the latest event time (ties: last stored)."""
        stmt = (
            select(MarketQuoteRecord)
            .where(
                MarketQuoteRecord.asset_id == asset_id,
                MarketQuoteRecord.feed == feed,
                MarketQuoteRecord.quality_status.in_(list(statuses)),
            )
            .order_by(MarketQuoteRecord.event_time.desc(), MarketQuoteRecord.id.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def latest_trade(
        self, asset_id: uuid.UUID, feed: str, statuses: Sequence[DataQualityStatus]
    ) -> MarketTradeRecord | None:
        """Trade with the latest event time (ties: last stored)."""
        stmt = (
            select(MarketTradeRecord)
            .where(
                MarketTradeRecord.asset_id == asset_id,
                MarketTradeRecord.feed == feed,
                MarketTradeRecord.quality_status.in_(list(statuses)),
            )
            .order_by(MarketTradeRecord.event_time.desc(), MarketTradeRecord.id.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class LiveUpdateRepository:
    """Rows stored since a cursor (primary keys only grow), for distributing updates
    to dashboard clients. Reads PostgreSQL only; never the provider."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def cursor(self) -> dict[str, int]:
        """Highest stored id per observation table (0 when empty)."""
        return {
            name: int(self._session.scalar(select(func.coalesce(func.max(model.id), 0))) or 0)
            for name, model in _UPDATE_TABLES.items()
        }

    def new_bars(
        self, after_id: int, upto_id: int, asset_ids: Sequence[uuid.UUID], feed: str, limit: int
    ) -> Sequence[MarketBarRecord]:
        stmt = (
            select(MarketBarRecord)
            .where(
                MarketBarRecord.id > after_id,
                MarketBarRecord.id <= upto_id,
                MarketBarRecord.asset_id.in_(list(asset_ids)),
                MarketBarRecord.feed == feed,
            )
            .order_by(MarketBarRecord.id)
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def bar_ids(
        self, after_id: int, upto_id: int, asset_ids: Sequence[uuid.UUID], feed: str
    ) -> Sequence[int]:
        stmt = select(MarketBarRecord.id).where(
            MarketBarRecord.id > after_id,
            MarketBarRecord.id <= upto_id,
            MarketBarRecord.asset_id.in_(list(asset_ids)),
            MarketBarRecord.feed == feed,
        )
        return self._session.scalars(stmt).all()

    def bar_ids_any_asset(self, after_id: int, upto_id: int) -> Sequence[int]:
        stmt = select(MarketBarRecord.id).where(
            MarketBarRecord.id > after_id, MarketBarRecord.id <= upto_id
        )
        return self._session.scalars(stmt).all()

    def bars_by_ids(self, ids: Sequence[int]) -> Sequence[MarketBarRecord]:
        if not ids:
            return []
        stmt = select(MarketBarRecord).where(MarketBarRecord.id.in_(list(ids)))
        return self._session.scalars(stmt.order_by(MarketBarRecord.id)).all()

    def latest_since(
        self,
        table: str,
        after_id: int,
        upto_id: int,
        asset_ids: Sequence[uuid.UUID],
        feed: str,
        statuses: Sequence[DataQualityStatus],
    ) -> list[tuple[MarketQuoteRecord | MarketTradeRecord, int]]:
        """Per asset: the last row stored in (after_id, upto_id] and how many arrived."""
        model = _UPDATE_TABLES[table]
        stats = self._session.execute(
            select(model.asset_id, func.max(model.id), func.count())
            .where(
                model.id > after_id,
                model.id <= upto_id,
                model.asset_id.in_(list(asset_ids)),
                model.feed == feed,
                model.quality_status.in_(list(statuses)),
            )
            .group_by(model.asset_id)
        ).all()
        if not stats:
            return []
        rows = {
            row.id: row
            for row in self._session.scalars(
                select(model).where(model.id.in_([max_id for _, max_id, _ in stats]))
            )
        }
        return [(rows[max_id], int(count)) for _, max_id, count in stats if max_id in rows]


_UPDATE_TABLES: dict[str, Any] = {
    "bars": MarketBarRecord,
    "quotes": MarketQuoteRecord,
    "trades": MarketTradeRecord,
}


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

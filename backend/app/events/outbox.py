"""The transactional outbox (spec 4.1 "Event processing"; Phase 7A).

1. A state change is committed to PostgreSQL; its event is written to ``outbox_events`` in
   the *same* transaction (:func:`enqueue`). Either both exist or neither does.
2. :class:`OutboxDispatcher` claims one pending event with ``FOR UPDATE SKIP LOCKED`` (so
   concurrent workers never take the same event), runs its handler inside a savepoint of
   that transaction and marks it DELIVERED, all in one commit.
3. A handler failure rolls back only the savepoint: the attempt, the error and the next
   attempt time are recorded with the row lock still held. After ``max_attempts`` the event
   is DEAD and the handler's ``on_dead`` recovery runs (also transactional).
4. A crash anywhere before COMMIT leaves the event PENDING and rolls back the handler's
   work: the next worker retries it from scratch.

Delivery is therefore at least once; handlers must be idempotent (every handler in Atlas
re-reads the authoritative state under its row locks and does nothing when the work is
already done). A second write of the same logical event is suppressed by the UNIQUE
``dedupe_key``. Redis plays no part: PostgreSQL is the only source of truth.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models.execution import OutboxEventRecord
from app.database.sessions import session_scope
from app.execution.enums import OutboxStatus

logger = get_logger(__name__)
MAX_ERROR = 1000


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    topic: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempts: int  # attempts made before this one


class OutboxHandler(Protocol):
    def handle(self, session: Session, message: OutboxMessage) -> None:
        """Do the work idempotently inside ``session`` (the caller commits)."""
        ...

    def on_dead(self, session: Session, message: OutboxMessage, error: str) -> None:
        """Recovery after the last failed attempt (the caller commits)."""
        ...


@dataclass
class DispatchResult:
    event_id: int
    topic: str
    status: OutboxStatus
    error: str | None = None


@dataclass
class DispatchSummary:
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    results: list[DispatchResult] = field(default_factory=list)


def enqueue(
    session: Session,
    *,
    topic: str,
    aggregate_type: str,
    aggregate_id: str | uuid.UUID,
    dedupe_key: str,
    payload: Mapping[str, Any],
    at: datetime,
) -> bool:
    """Record an event in the caller's transaction. Returns False when an event with the
    same ``dedupe_key`` already exists (a duplicate is suppressed, never re-sent)."""
    inserted = session.execute(
        pg_insert(OutboxEventRecord)
        .values(
            topic=topic,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            dedupe_key=dedupe_key,
            payload=dict(payload),
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=at,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(OutboxEventRecord.id)
    ).scalar()
    return inserted is not None


def _describe(exc: BaseException) -> str:
    """Type and message only (no traceback, no bound parameters)."""
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= MAX_ERROR else text[: MAX_ERROR - 3] + "..."


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        handlers: Mapping[str, OutboxHandler],
        *,
        max_attempts: int = 5,
        base_backoff: timedelta = timedelta(seconds=5),
        max_backoff: timedelta = timedelta(minutes=10),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._factory = session_factory
        self._clock = clock
        self._handlers = dict(handlers)
        self.max_attempts = max_attempts
        self._base = base_backoff
        self._max = max_backoff

    @property
    def topics(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def backoff(self, attempts: int) -> timedelta:
        """Exponential: base, 2 x base, 4 x base, ... capped."""
        delay: timedelta = self._base * (2 ** max(attempts - 1, 0))
        return delay if delay < self._max else self._max

    def dispatch_once(self, *, topics: frozenset[str] | None = None) -> DispatchResult | None:
        """Deliver at most one due event. None when nothing is due."""
        wanted = self.topics if topics is None else topics & self.topics
        if not wanted:
            return None
        with session_scope(self._factory) as session:
            now = self._clock.now()
            row = session.scalar(
                select(OutboxEventRecord)
                .where(
                    OutboxEventRecord.status == OutboxStatus.PENDING,
                    OutboxEventRecord.available_at <= now,
                    OutboxEventRecord.topic.in_(sorted(wanted)),
                )
                .order_by(OutboxEventRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            message = OutboxMessage(
                row.id,
                row.topic,
                row.aggregate_type,
                row.aggregate_id,
                dict(row.payload),
                row.attempts,
            )
            handler = self._handlers[row.topic]
            try:
                with session.begin_nested():
                    handler.handle(session, message)
            except Exception as exc:  # recorded and retried, never swallowed
                return self._failed(session, row, handler, message, exc, now)
            row.status = OutboxStatus.DELIVERED
            row.attempts += 1
            row.delivered_at = now
            row.updated_at = func.now()
            session.flush()
            return DispatchResult(row.id, row.topic, OutboxStatus.DELIVERED)

    def _failed(
        self,
        session: Session,
        row: OutboxEventRecord,
        handler: OutboxHandler,
        message: OutboxMessage,
        exc: Exception,
        now: datetime,
    ) -> DispatchResult:
        error = _describe(exc)
        row.attempts += 1
        row.last_error = error
        row.updated_at = func.now()
        logger.warning(
            "outbox.delivery_failed",
            event_id=row.id,
            topic=row.topic,
            attempts=row.attempts,
            error_type=type(exc).__name__,
        )
        if row.attempts < self.max_attempts:
            row.available_at = now + self.backoff(row.attempts)
            session.flush()
            return DispatchResult(row.id, row.topic, OutboxStatus.PENDING, error)
        row.status = OutboxStatus.DEAD
        row.dead_at = now
        session.flush()
        try:
            with session.begin_nested():
                handler.on_dead(session, message, error)
        except Exception as dead_exc:
            # The event stays DEAD; the recovery job reconciles what is left.
            row.last_error = _describe(dead_exc)
            session.flush()
            logger.error("outbox.recovery_failed", event_id=row.id, topic=row.topic)
        logger.error("outbox.dead", event_id=row.id, topic=row.topic, attempts=row.attempts)
        return DispatchResult(row.id, row.topic, OutboxStatus.DEAD, error)

    def dispatch(
        self, *, limit: int = 100, topics: frozenset[str] | None = None
    ) -> DispatchSummary:
        """Deliver due events until none is left or ``limit`` were handled."""
        summary = DispatchSummary()
        for _ in range(limit):
            result = self.dispatch_once(topics=topics)
            if result is None:
                break
            summary.results.append(result)
            if result.status is OutboxStatus.DELIVERED:
                summary.delivered += 1
            elif result.status is OutboxStatus.DEAD:
                summary.dead += 1
            else:
                summary.failed += 1
        return summary


def backlog(session: Session) -> dict[str, Any]:
    """Counts by status, the oldest pending event and the number of dead events."""
    rows = session.execute(
        select(
            OutboxEventRecord.status, func.count(), func.min(OutboxEventRecord.created_at)
        ).group_by(OutboxEventRecord.status)
    ).all()
    counts = {s.value: 0 for s in OutboxStatus}
    oldest = None
    for status, count, first in rows:
        counts[OutboxStatus(status).value] = int(count)
        if OutboxStatus(status) is OutboxStatus.PENDING:
            oldest = first
    return {
        "counts": counts,
        "oldest_pending_at": None if oldest is None else oldest.isoformat(),
    }


__all__ = [
    "DispatchResult",
    "DispatchSummary",
    "OutboxDispatcher",
    "OutboxHandler",
    "OutboxMessage",
    "backlog",
    "enqueue",
]

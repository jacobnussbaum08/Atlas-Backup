"""Durable worker heartbeats (Phase 10B, ``worker_heartbeats``).

A worker process registers one row (worker name, instance id) at start, renews it after
every pass, and records STOPPED on a clean stop. A crashed or hung process simply stops
renewing it: its age is the truth the dashboard and the orchestrator's protection gate read
(D-213). A database outage only delays the next write; it never stops the worker.

Readers take the **newest** heartbeat of a worker across its instances. Stale rows of dead
instances are left as they are (they are the record of when each instance last ran).
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.database.models.reliability import WorkerHeartbeatRecord
from app.database.sessions import session_scope
from app.orchestration.enums import WorkerState

logger = get_logger(__name__)

POSITION_MONITOR = "position-monitor-worker"
# A heartbeat is stale after this many missed passes (never less than STALE_FLOOR).
MISSED_PASSES = 6
STALE_FLOOR = timedelta(seconds=60)


def instance_id() -> str:
    return f"{socket.gethostname()[:48]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def stale_after(interval_seconds: float | Decimal | None) -> timedelta:
    if interval_seconds is None or interval_seconds <= 0:
        return STALE_FLOOR
    return max(STALE_FLOOR, timedelta(seconds=MISSED_PASSES * float(interval_seconds)))


class HeartbeatWriter:
    """Writes one worker instance's heartbeat. Every method swallows database errors (a
    worker must keep protecting positions through an outage) and logs their type only."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        worker: str,
        *,
        interval_seconds: float | None,
        instance: str | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self.worker = worker
        self.instance = instance or instance_id()
        self.interval = None if interval_seconds is None else Decimal(str(interval_seconds))
        self.failures = 0

    def _write(self, values: dict[str, Any]) -> bool:
        try:
            with session_scope(self._factory) as session:
                session.execute(
                    pg_insert(WorkerHeartbeatRecord)
                    .values(worker=self.worker, instance_id=self.instance, **values)
                    .on_conflict_do_update(
                        index_elements=["worker", "instance_id"],
                        set_={k: v for k, v in values.items() if k != "started_at"},
                    )
                )
            return True
        except Exception as exc:  # the database may be down: keep working
            self.failures += 1
            logger.warning(
                "heartbeat.unrecorded", worker=self.worker, error_type=type(exc).__name__
            )
            return False

    def start(self, details: dict[str, Any] | None = None) -> bool:
        now = self._clock.now()
        return self._write(
            {
                "state": WorkerState.STARTING,
                "interval_seconds": self.interval,
                "started_at": now,
                "heartbeat_at": now,
                "stopped_at": None,
                "details": details or {},
            }
        )

    def beat(
        self,
        *,
        passes: int,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        now = self._clock.now()
        values: dict[str, Any] = {
            "state": WorkerState.DEGRADED if error else WorkerState.RUNNING,
            "interval_seconds": self.interval,
            "started_at": now,  # only used if the row was lost; never overwrites
            "heartbeat_at": now,
            "stopped_at": None,
            "passes": passes,
            "details": details or {},
        }
        if error:
            values["last_error"] = redact_text(error)[:1000]
            values["last_error_at"] = now
        return self._write(values)

    def stop(self, *, passes: int) -> bool:
        now = self._clock.now()
        return self._write(
            {
                "state": WorkerState.STOPPED,
                "interval_seconds": self.interval,
                "started_at": now,
                "heartbeat_at": now,
                "stopped_at": now,
                "passes": passes,
            }
        )


@dataclass(frozen=True)
class Liveness:
    """What the newest heartbeat of a worker says at ``now``."""

    worker: str
    alive: bool
    state: str | None
    heartbeat_at: datetime | None
    age_seconds: float | None
    stale_after_seconds: float
    instance_id: str | None
    detail: str


def latest(session: Session, worker: str) -> WorkerHeartbeatRecord | None:
    return session.scalars(
        select(WorkerHeartbeatRecord)
        .where(WorkerHeartbeatRecord.worker == worker)
        .order_by(WorkerHeartbeatRecord.heartbeat_at.desc())
        .limit(1)
    ).first()


def liveness(session: Session, worker: str, now: datetime) -> Liveness:
    row = latest(session, worker)
    if row is None:
        return Liveness(
            worker, False, None, None, None, STALE_FLOOR.total_seconds(), None, "never reported"
        )
    limit = stale_after(row.interval_seconds)
    age = (now - row.heartbeat_at).total_seconds()
    if row.state is WorkerState.STOPPED:
        alive, detail = False, "recorded a clean stop"
    elif age > limit.total_seconds():
        alive, detail = (
            False,
            f"no heartbeat for {int(age)} s (limit {int(limit.total_seconds())} s)",
        )
    else:
        alive, detail = True, f"heartbeat {int(age)} s ago ({row.state.value})"
    return Liveness(
        worker,
        alive,
        row.state.value,
        row.heartbeat_at,
        round(age, 1),
        limit.total_seconds(),
        row.instance_id,
        detail,
    )


__all__ = [
    "MISSED_PASSES",
    "POSITION_MONITOR",
    "STALE_FLOOR",
    "HeartbeatWriter",
    "Liveness",
    "instance_id",
    "latest",
    "liveness",
    "stale_after",
]

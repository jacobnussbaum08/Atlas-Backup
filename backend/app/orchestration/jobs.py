"""Durable scheduled jobs (Phase 10A).

A job is one (kind, slot) row in ``orchestration_jobs`` (UNIQUE): the slot names the period
it covers (an interval start, or a trading day for after-close jobs), so a slot runs once
however many orchestrators tick or restart.

* **Claim** (one transaction): insert the row RUNNING with a lease, or take over an existing
  row of the current slot that FAILED and is due for a retry, or whose lease expired (a
  **stuck** job: its worker stopped or hung; counted in ``reclaimed`` and reported).
* **Run** outside that transaction; the job's own services use their own transactions
  and are idempotent (monitoring, reconciliation, evaluation).
* **Finish**: SUCCEEDED with a result, or FAILED with the error and the next attempt
  (exponential back-off), or DEAD after the maximum attempts (``system_health_events``).
* **Sweep**: a RUNNING job of an older slot whose lease expired is never reclaimed by its
  slot function again; the sweep finishes it FAILED (or DEAD) and reports it.

PostgreSQL holds the job state; nothing is kept only in memory or in Redis.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.calendar import local_date, trading_day
from app.database.models.orchestration import OrchestrationJobRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.orchestration.enums import JobKind, JobStatus
from app.risk.enums import Severity

logger = get_logger(__name__)
MAX_ERROR = 1000
BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=15)

SlotFn = Callable[[datetime], str | None]


def every(seconds: int) -> SlotFn:
    """Slot = the start of the fixed interval containing ``at`` (UTC epoch aligned)."""

    def slot(at: datetime) -> str:
        start = int(at.timestamp()) // seconds * seconds
        return datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return slot


def after_close(delay_seconds: int) -> SlotFn:
    """Slot = the trading day, from its regular close plus ``delay_seconds`` until the end
    of that New York day; None on closed days and before the time."""

    def slot(at: datetime) -> str | None:
        day = trading_day(local_date(at))
        if day is None or at < day.regular_close + timedelta(seconds=delay_seconds):
            return None
        return f"close:{day.day.isoformat()}"

    return slot


@dataclass(frozen=True)
class JobSpec:
    kind: JobKind
    slot: SlotFn
    run: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class JobOutcome:
    kind: JobKind
    slot: str
    status: JobStatus
    attempts: int
    reclaimed: bool = False
    error: str | None = None


def _describe(exc: BaseException) -> str:
    text = redact_text(f"{type(exc).__name__}: {exc}")
    return text if len(text) <= MAX_ERROR else text[: MAX_ERROR - 3] + "..."


def backoff(attempts: int) -> timedelta:
    delay: timedelta = BASE_BACKOFF * (2 ** max(attempts - 1, 0))
    return delay if delay < MAX_BACKOFF else MAX_BACKOFF


class JobRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        *,
        owner: str,
        lease: timedelta,
        max_attempts: int,
        account_id: Any = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self.owner = owner
        self._lease = lease
        self._max = max_attempts
        self._account = account_id

    # ------------------------------------------------------------------ claim
    def claim(self, kind: JobKind, slot: str) -> tuple[int, int, bool] | None:
        """(job id, attempt number, reclaimed) when this worker may run the slot now."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            inserted = session.execute(
                pg_insert(OrchestrationJobRecord)
                .values(
                    kind=kind,
                    slot=slot,
                    portfolio_account_id=self._account,
                    status=JobStatus.RUNNING,
                    attempts=1,
                    lease_owner=self.owner,
                    lease_expires_at=now + self._lease,
                    heartbeat_at=now,
                    started_at=now,
                )
                .on_conflict_do_nothing(index_elements=["kind", "slot"])
                .returning(OrchestrationJobRecord.id)
            ).scalar()
            if inserted is not None:
                return int(inserted), 1, False
            row = session.scalar(
                select(OrchestrationJobRecord)
                .where(OrchestrationJobRecord.kind == kind, OrchestrationJobRecord.slot == slot)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None  # another worker holds it right now
            stuck = (
                row.status is JobStatus.RUNNING
                and row.lease_expires_at is not None
                and row.lease_expires_at < now
            )
            retry = (
                row.status is JobStatus.FAILED
                and row.attempts < self._max
                and (row.next_attempt_at is None or row.next_attempt_at <= now)
            )
            if not (stuck or retry):
                return None
            if stuck:
                row.reclaimed += 1
                self._health(
                    session,
                    "ORCHESTRATION_JOB_STUCK",
                    Severity.WARNING,
                    row,
                    f"{kind.value} {slot}: lease of {row.lease_owner} expired at "
                    f"{row.lease_expires_at.isoformat() if row.lease_expires_at else '?'}; "
                    "reclaimed",
                    now,
                )
                if row.attempts >= self._max:
                    self._dead(session, row, "lease expired on the last attempt", now)
                    return None
            row.status = JobStatus.RUNNING
            row.attempts += 1
            row.lease_owner = self.owner
            row.lease_expires_at = now + self._lease
            row.heartbeat_at = now
            row.started_at = now
            row.next_attempt_at = None
            session.flush()
            return int(row.id), int(row.attempts), stuck

    # ------------------------------------------------------------------ finish
    def _finish(
        self, job_id: int, result: dict[str, Any] | None, exc: Exception | None
    ) -> JobOutcome:
        with session_scope(self._factory) as session:
            now = self._clock.now()
            row = session.scalar(
                select(OrchestrationJobRecord)
                .where(OrchestrationJobRecord.id == job_id)
                .with_for_update()
            )
            assert row is not None  # noqa: S101  # claimed by us
            if row.lease_owner != self.owner or row.status is not JobStatus.RUNNING:
                # Our lease expired and someone else took the job over: their result counts.
                return JobOutcome(row.kind, row.slot, row.status, row.attempts)
            row.heartbeat_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            if exc is None:
                row.status = JobStatus.SUCCEEDED
                row.finished_at = now
                row.result = result or {}
                row.error = None
            else:
                row.error = _describe(exc)
                if row.attempts >= self._max:
                    self._dead(session, row, row.error, now)
                else:
                    row.status = JobStatus.FAILED
                    row.next_attempt_at = now + backoff(row.attempts)
            session.flush()
            return JobOutcome(row.kind, row.slot, row.status, row.attempts, error=row.error)

    def _dead(
        self, session: Session, row: OrchestrationJobRecord, error: str, now: datetime
    ) -> None:
        row.status = JobStatus.DEAD
        row.finished_at = now
        row.lease_owner = None
        row.lease_expires_at = None
        row.error = error
        self._health(
            session,
            "ORCHESTRATION_JOB_DEAD",
            Severity.HIGH,
            row,
            f"{row.kind.value} {row.slot} failed {row.attempts} attempt(s): {error[:200]}",
            now,
        )

    def _health(
        self,
        session: Session,
        event_type: str,
        severity: Severity,
        row: OrchestrationJobRecord,
        summary: str,
        now: datetime,
    ) -> None:
        session.add(
            SystemHealthEventRecord(
                component="orchestration",
                event_type=event_type,
                severity=severity,
                portfolio_account_id=row.portfolio_account_id,
                subject_kind="orchestration_job",
                subject_id=str(row.id),
                summary=summary,
                details={"kind": row.kind.value, "slot": row.slot, "attempts": row.attempts},
                occurred_at=now,
            )
        )
        logger.warning(
            "orchestration.job_" + event_type.lower(), kind=row.kind.value, slot=row.slot
        )

    # ------------------------------------------------------------------ run
    def run_due(self, specs: list[JobSpec]) -> list[JobOutcome]:
        outcomes: list[JobOutcome] = []
        for spec in specs:
            slot = spec.slot(self._clock.now())
            if slot is None:
                continue
            claimed = self.claim(spec.kind, slot)
            if claimed is None:
                continue
            job_id, _attempt, reclaimed = claimed
            try:
                result = spec.run()
            except Exception as exc:  # recorded and retried, never swallowed
                logger.warning(
                    "orchestration.job_failed", kind=spec.kind.value, error_type=type(exc).__name__
                )
                outcome = self._finish(job_id, None, exc)
            else:
                outcome = self._finish(job_id, result, None)
            outcomes.append(
                JobOutcome(
                    outcome.kind,
                    outcome.slot,
                    outcome.status,
                    outcome.attempts,
                    reclaimed,
                    outcome.error,
                )
            )
        return outcomes

    def sweep(self) -> int:
        """Finish RUNNING jobs whose lease expired (stopped workers): FAILED, or DEAD on
        their last attempt. Their slot's function may never offer them again."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            rows = session.scalars(
                select(OrchestrationJobRecord)
                .where(
                    OrchestrationJobRecord.status == JobStatus.RUNNING,
                    OrchestrationJobRecord.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            ).all()
            for row in rows:
                owner = row.lease_owner
                row.reclaimed += 1
                row.error = f"lease of {owner} expired: the worker stopped or hung"
                self._health(
                    session, "ORCHESTRATION_JOB_STUCK", Severity.WARNING, row, row.error, now
                )
                if row.attempts >= self._max:
                    self._dead(session, row, row.error, now)
                else:
                    row.status = JobStatus.FAILED
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.next_attempt_at = now
            session.flush()
            return len(rows)


__all__ = ["JobOutcome", "JobRunner", "JobSpec", "after_close", "backoff", "every"]

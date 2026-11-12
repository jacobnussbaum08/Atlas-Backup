"""Durable scheduled jobs (Phases 10A-10B).

A job is one (kind, slot) row in ``orchestration_jobs`` (UNIQUE): the slot names the period
it covers (an interval start, or a trading day for daily jobs), so a slot runs once however
many orchestrators tick or restart.

* **Claim** (one transaction): insert the row RUNNING with a lease, or take over an existing
  row of the slot that FAILED and is due for a retry, or whose lease expired (a **stuck**
  job: its worker stopped or hung; counted in ``reclaimed`` and reported).
* **No overlap** (10B): a kind never runs in two slots at once. While a run of an earlier
  slot still holds a live lease, the next slot waits (D-215).
* **Run** outside that transaction; the job's own services use their own transactions
  and are idempotent (monitoring, reconciliation, evaluation). While it runs, a keeper
  thread renews the lease (10B), so a long job is not mistaken for a stuck one.
* **Finish**: SUCCEEDED with a result, or FAILED with the error and the next attempt
  (exponential back-off), or DEAD after the maximum attempts (a dead letter:
  ``system_health_events``; an operator may requeue it).
* **Missed slots** (10B, D-216): a daily job (one slot per trading day) whose day was missed
  during an outage is caught up after the restart, within ``catch_up_days`` trading days:
  a per-day job (the session review) runs each missed day; other daily jobs run once for
  the newest missed day and the older missed days are recorded SUPERSEDED. Interval jobs
  are never back-filled (a past risk check is meaningless now); the first slot after a gap
  reports how many slots were missed (``ORCHESTRATION_SCHEDULE_GAP``).
* **Sweep**: a RUNNING job of an older slot whose lease expired is never reclaimed by its
  slot function again; the sweep finishes it FAILED (or DEAD) and reports it. A FAILED job
  that will never be offered again because a later slot of its kind has SUCCEEDED becomes
  SUPERSEDED.

PostgreSQL holds the job state; nothing is kept only in memory or in Redis.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.calendar import local_date, trading_day
from app.database.models.audit_log import ActorType, AuditLog
from app.database.models.orchestration import OrchestrationJobRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.orchestration.enums import FINAL_JOB_STATUSES, JobKind, JobStatus
from app.risk.enums import Severity

logger = get_logger(__name__)
MAX_ERROR = 1000
BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=15)

SlotFn = Callable[[datetime], str | None]
# Earlier slots of a daily job, oldest first (``days`` trading days before ``at``'s).
PastFn = Callable[[datetime, int], list[str]]


class JobControlError(ValueError):
    """An operator action on a job was refused (the message says why)."""


def every(seconds: int) -> SlotFn:
    """Slot = the start of the fixed interval containing ``at`` (UTC epoch aligned)."""

    def slot(at: datetime) -> str:
        start = int(at.timestamp()) // seconds * seconds
        return datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return slot


def slot_start(slot: str) -> datetime | None:
    """The interval start an ``every`` slot names (None for other slots)."""
    try:
        return datetime.strptime(slot, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _day_slot(prefix: str, day: date) -> str:
    return f"{prefix}:{day.isoformat()}"


def slot_day(slot: str) -> date | None:
    """The trading day a daily slot (``<prefix>:<YYYY-MM-DD>``) names."""
    _, _, tail = slot.partition(":")
    try:
        return date.fromisoformat(tail)
    except ValueError:
        return None


def after_close(delay_seconds: int, prefix: str = "close") -> SlotFn:
    """Slot = the trading day, from its regular close plus ``delay_seconds`` until the end
    of that New York day; None on closed days and before the time."""

    def slot(at: datetime) -> str | None:
        day = trading_day(local_date(at))
        if day is None or at < day.regular_close + timedelta(seconds=delay_seconds):
            return None
        return _day_slot(prefix, day.day)

    return slot


def around_open(lead_seconds: int, window_seconds: int, prefix: str = "open") -> SlotFn:
    """Slot = the trading day, from ``lead_seconds`` before its regular open until
    ``window_seconds`` after it; None otherwise."""

    def slot(at: datetime) -> str | None:
        day = trading_day(local_date(at))
        if day is None:
            return None
        start = day.regular_open - timedelta(seconds=lead_seconds)
        if not start <= at < day.regular_open + timedelta(seconds=window_seconds):
            return None
        return _day_slot(prefix, day.day)

    return slot


def previous_days(prefix: str) -> PastFn:
    """The ``days`` trading days before ``at``'s New York date, as slots, oldest first."""

    def past(at: datetime, days: int) -> list[str]:
        out: list[str] = []
        cursor = local_date(at)
        for _ in range(days * 3 + 10):  # weekends and holidays
            if len(out) >= days:
                break
            cursor = cursor - timedelta(days=1)
            day = trading_day(cursor)
            if day is not None:
                out.append(_day_slot(prefix, day.day))
        return list(reversed(out))

    return past


@dataclass(frozen=True)
class JobSpec:
    kind: JobKind
    slot: SlotFn
    run: Callable[[], dict[str, Any]]
    # Interval jobs: the interval (missed-slot detection). Daily jobs: the earlier slots.
    interval_seconds: int | None = None
    past: PastFn | None = None
    # Daily jobs: True = every missed day is run on its own (its body takes the slot).
    per_slot: bool = False
    run_slot: Callable[[str], dict[str, Any]] | None = None


@dataclass(frozen=True)
class JobOutcome:
    kind: JobKind
    slot: str
    status: JobStatus
    attempts: int
    reclaimed: bool = False
    error: str | None = None
    caught_up: bool = False


def _describe(exc: BaseException) -> str:
    text = redact_text(f"{type(exc).__name__}: {exc}")
    return text if len(text) <= MAX_ERROR else text[: MAX_ERROR - 3] + "..."


def backoff(attempts: int) -> timedelta:
    delay: timedelta = BASE_BACKOFF * (2 ** max(attempts - 1, 0))
    return delay if delay < MAX_BACKOFF else MAX_BACKOFF


class _LeaseKeeper:
    """Renews a running job's lease every lease/3 until stopped."""

    def __init__(self, runner: JobRunner, job_id: int) -> None:
        self._runner = runner
        self._job = job_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"job-lease-{job_id}", daemon=True)
        self.lost = False

    def _loop(self) -> None:
        interval = max(self._runner.lease.total_seconds() / 3, 1.0)
        while not self._stop.wait(interval):
            try:
                if not self._runner.renew(self._job):
                    self.lost = True
                    return
            except Exception as exc:  # the database is down: the next renewal retries
                logger.warning("orchestration.lease_renew_failed", error_type=type(exc).__name__)

    def __enter__(self) -> _LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


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
        catch_up_days: int = 3,
        renew_leases: bool = True,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self.owner = owner
        self.lease = lease
        self._max = max_attempts
        self._account = account_id
        self.catch_up_days = catch_up_days
        self._renew_leases = renew_leases

    # ------------------------------------------------------------------ claim
    def claim(
        self, kind: JobKind, slot: str, *, interval_seconds: int | None = None
    ) -> tuple[int, int, bool] | None:
        """(job id, attempt number, reclaimed) when this worker may run the slot now."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            busy = session.scalar(
                select(OrchestrationJobRecord.id).where(
                    OrchestrationJobRecord.kind == kind,
                    OrchestrationJobRecord.slot != slot,
                    OrchestrationJobRecord.status == JobStatus.RUNNING,
                    OrchestrationJobRecord.lease_expires_at >= now,
                )
            )
            if busy is not None:
                return None  # an earlier slot of this kind is still running: no overlap
            inserted = session.execute(
                pg_insert(OrchestrationJobRecord)
                .values(
                    kind=kind,
                    slot=slot,
                    portfolio_account_id=self._account,
                    status=JobStatus.RUNNING,
                    attempts=1,
                    lease_owner=self.owner,
                    lease_expires_at=now + self.lease,
                    heartbeat_at=now,
                    started_at=now,
                )
                .on_conflict_do_nothing(index_elements=["kind", "slot"])
                .returning(OrchestrationJobRecord.id)
            ).scalar()
            if inserted is not None:
                if interval_seconds:
                    self._report_gap(session, kind, slot, interval_seconds, now)
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
            row.lease_expires_at = now + self.lease
            row.heartbeat_at = now
            row.started_at = now
            row.next_attempt_at = None
            session.flush()
            return int(row.id), int(row.attempts), stuck

    def _report_gap(
        self, session: Session, kind: JobKind, slot: str, interval: int, now: datetime
    ) -> None:
        """The first slot after a gap of an interval job: how many slots were missed."""
        start = slot_start(slot)
        previous = session.scalar(
            select(OrchestrationJobRecord.slot)
            .where(OrchestrationJobRecord.kind == kind, OrchestrationJobRecord.slot < slot)
            .order_by(OrchestrationJobRecord.slot.desc())
            .limit(1)
        )
        before = None if previous is None else slot_start(previous)
        if start is None or before is None:
            return
        missed = int((start - before).total_seconds() // interval) - 1
        if missed < 1:
            return
        session.add(
            SystemHealthEventRecord(
                component="orchestration",
                event_type="ORCHESTRATION_SCHEDULE_GAP",
                severity=Severity.WARNING,
                portfolio_account_id=self._account,
                subject_kind="orchestration_job_kind",
                subject_id=kind.value,
                summary=f"{kind.value}: {missed} slot(s) between {previous} and {slot} did not "
                "run (the orchestrator was not running); interval jobs are not back-filled",
                details={"kind": kind.value, "previous": previous, "slot": slot, "missed": missed},
                occurred_at=now,
            )
        )
        logger.warning("orchestration.schedule_gap", kind=kind.value, missed=missed)

    def renew(self, job_id: int) -> bool:
        """Extend a running job's lease. False when this worker no longer holds it."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            row = session.get(OrchestrationJobRecord, job_id, with_for_update=True)
            if row is None or row.status is not JobStatus.RUNNING or row.lease_owner != self.owner:
                return False
            row.lease_expires_at = now + self.lease
            row.heartbeat_at = now
            return True

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
                row.result = {**(row.result or {}), **(result or {})}
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
    def _execute(self, spec: JobSpec, slot: str, *, caught_up: bool) -> JobOutcome | None:
        claimed = self.claim(spec.kind, slot, interval_seconds=spec.interval_seconds)
        if claimed is None:
            return None
        job_id, _attempt, reclaimed = claimed
        body: Callable[[], dict[str, Any]] = spec.run
        if spec.per_slot and spec.run_slot is not None:
            run_slot = spec.run_slot

            def body() -> dict[str, Any]:
                return run_slot(slot)

        try:
            if self._renew_leases:
                with _LeaseKeeper(self, job_id):
                    result = body()
            else:
                result = body()
        except Exception as exc:  # recorded and retried, never swallowed
            logger.warning(
                "orchestration.job_failed", kind=spec.kind.value, error_type=type(exc).__name__
            )
            outcome = self._finish(job_id, None, exc)
        else:
            if caught_up:
                result = {**(result or {}), "caught_up": True}
            outcome = self._finish(job_id, result, None)
        return JobOutcome(
            outcome.kind,
            outcome.slot,
            outcome.status,
            outcome.attempts,
            reclaimed,
            outcome.error,
            caught_up,
        )

    def _rows(self, session: Session, kind: JobKind, slots: list[str]) -> dict[str, Any]:
        if not slots:
            return {}
        rows = session.scalars(
            select(OrchestrationJobRecord).where(
                OrchestrationJobRecord.kind == kind, OrchestrationJobRecord.slot.in_(slots)
            )
        ).all()
        return {r.slot: r for r in rows}

    def _supersede_missed(self, kind: JobKind, slots: list[str], covered_by: str) -> None:
        """Record missed daily slots that are covered by a later run (no row existed)."""
        if not slots:
            return
        with session_scope(self._factory) as session:
            now = self._clock.now()
            for slot in slots:
                session.execute(
                    pg_insert(OrchestrationJobRecord)
                    .values(
                        kind=kind,
                        slot=slot,
                        portfolio_account_id=self._account,
                        status=JobStatus.SUPERSEDED,
                        attempts=0,
                        finished_at=now,
                        error=f"missed while the orchestrator was not running; covered by "
                        f"the run for {covered_by}",
                    )
                    .on_conflict_do_nothing(index_elements=["kind", "slot"])
                )

    def _daily_candidates(self, spec: JobSpec, current: str | None) -> list[tuple[str, bool]]:
        """(slot, caught_up) to try for a daily job, oldest first."""
        if spec.past is None or self.catch_up_days <= 0:
            return [] if current is None else [(current, False)]
        past = spec.past(self._clock.now(), self.catch_up_days)
        if current is not None:
            past = [s for s in past if s < current]
        with session_scope(self._factory) as session:
            rows = self._rows(session, spec.kind, past)
            pending = [
                s
                for s in past
                if s not in rows
                or (rows[s].status is JobStatus.FAILED and rows[s].attempts < self._max)
                or rows[s].status is JobStatus.RUNNING
            ]
            missing = [s for s in past if s not in rows]
        if spec.per_slot:
            out = [(s, True) for s in pending]
        elif current is not None:
            self._supersede_missed(spec.kind, missing, current)
            out = []
        elif pending:
            newest = pending[-1]
            self._supersede_missed(spec.kind, [s for s in missing if s != newest], newest)
            out = [(newest, True)]
        else:
            out = []
        if current is not None:
            out.append((current, False))
        return out

    def run_due(self, specs: list[JobSpec]) -> list[JobOutcome]:
        outcomes: list[JobOutcome] = []
        for spec in specs:
            current = spec.slot(self._clock.now())
            if spec.past is not None:
                candidates = self._daily_candidates(spec, current)
            else:
                candidates = [] if current is None else [(current, False)]
            for slot, caught_up in candidates:
                outcome = self._execute(spec, slot, caught_up=caught_up)
                if outcome is not None:
                    outcomes.append(outcome)
            if current is not None and spec.past is None:
                self._supersede_failed(spec.kind)
        return outcomes

    def _supersede_failed(self, kind: JobKind) -> int:
        """FAILED interval jobs of earlier slots of a kind that has since succeeded: they
        will never be offered again."""
        with session_scope(self._factory) as session:
            now = self._clock.now()
            newest_ok = session.scalar(
                select(OrchestrationJobRecord.slot)
                .where(
                    OrchestrationJobRecord.kind == kind,
                    OrchestrationJobRecord.status == JobStatus.SUCCEEDED,
                )
                .order_by(OrchestrationJobRecord.slot.desc())
                .limit(1)
            )
            if newest_ok is None:
                return 0
            rows = session.scalars(
                select(OrchestrationJobRecord)
                .where(
                    OrchestrationJobRecord.kind == kind,
                    OrchestrationJobRecord.status == JobStatus.FAILED,
                    OrchestrationJobRecord.slot < newest_ok,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            ).all()
            for row in rows:
                row.status = JobStatus.SUPERSEDED
                row.finished_at = now
                row.next_attempt_at = None
                row.error = f"{row.error or 'failed'}; superseded by the run for {newest_ok}"
            return len(rows)

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

    # ------------------------------------------------------------------ dead letters
    def requeue(self, job_id: int, *, actor: str, reason: str) -> OrchestrationJobRecord:
        """An operator requeues a DEAD job: it becomes FAILED with a fresh attempt budget,
        due now. The previous attempts and error are kept in ``result.requeued_from``; the
        requeue is audited and reported. Only DEAD jobs can be requeued."""
        if not actor.startswith("operator:") or len(actor) <= len("operator:"):
            raise JobControlError("only an operator (operator:<name>) may requeue a job")
        if len(reason.strip()) < 3:
            raise JobControlError("a reason is required")
        with session_scope(self._factory) as session:
            now = self._clock.now()
            row = session.get(OrchestrationJobRecord, job_id, with_for_update=True)
            if row is None:
                raise JobControlError(f"job {job_id} does not exist")
            if row.status is not JobStatus.DEAD:
                raise JobControlError(f"job {job_id} is {row.status.value}; only DEAD jobs requeue")
            history = list((row.result or {}).get("requeued_from", []))
            history.append(
                {
                    "attempts": row.attempts,
                    "error": row.error,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "by": actor,
                    "at": now.isoformat(),
                    "reason": reason.strip()[:500],
                }
            )
            row.result = {**(row.result or {}), "requeued_from": history}
            row.status = JobStatus.FAILED
            row.attempts = 0
            row.finished_at = None
            row.next_attempt_at = now
            row.requeued_at = now
            row.requeued_by = actor
            session.add(
                AuditLog(
                    actor_type=ActorType.USER,
                    actor_id=actor,
                    action="orchestration.job.requeue",
                    entity_type="orchestration_job",
                    entity_id=str(row.id),
                    reason=reason.strip()[:500],
                    details={"kind": row.kind.value, "slot": row.slot},
                )
            )
            self._health(
                session,
                "ORCHESTRATION_JOB_REQUEUED",
                Severity.INFO,
                row,
                f"{row.kind.value} {row.slot} requeued by {actor}: {reason.strip()[:200]}",
                now,
            )
            session.flush()
            session.expunge(row)
            return row

    @staticmethod
    def is_final(status: JobStatus) -> bool:
        return status in FINAL_JOB_STATUSES


__all__ = [
    "JobControlError",
    "JobOutcome",
    "JobRunner",
    "JobSpec",
    "after_close",
    "around_open",
    "backoff",
    "every",
    "previous_days",
    "slot_day",
    "slot_start",
]

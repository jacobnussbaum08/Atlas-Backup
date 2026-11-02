"""Durable discovery jobs: creation, claiming, leases, retries and recovery (Phase 3A).

* **No overlap.** A partial unique index allows one PENDING or RUNNING job per kind and
  provider. Requesting a job while one is active returns the active job instead
  (idempotent requests); two racing requests produce one job.
* **Claiming.** ``claim_next`` locks a due PENDING job with ``FOR UPDATE SKIP LOCKED`` and
  sets it RUNNING with a lease in the same transaction, so the API's background runner
  and the worker can never run the same job twice.
* **Crash recovery.** A RUNNING job whose lease expired (its process died) is returned
  to PENDING for another attempt, or FAILED once its attempts are used up. Every job step
  is idempotent, so a re-run never duplicates instruments, versions or membership.
* **Retries.** A transient failure (provider outage, database interruption) re-queues
  the job with exponential backoff; a permanent one (rejected credentials, unsupported
  capability) fails it at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.asset_discovery.enums import (
    ACTIVE_JOB_STATUSES,
    DiscoveryJobKind,
    DiscoveryJobStatus,
    JobTrigger,
)
from app.core.security.redaction import redact_text
from app.database.models import DiscoveryJobRecord as Job

MAX_RECENT_ERRORS = 10
RETRY_BASE = timedelta(minutes=1)
RETRY_MAX = timedelta(hours=1)
FINISHED = (
    DiscoveryJobStatus.COMPLETED,
    DiscoveryJobStatus.PARTIALLY_COMPLETED,
    DiscoveryJobStatus.FAILED,
)


class JobStateError(ValueError):
    pass


@dataclass(frozen=True)
class CreatedJob:
    job: Job
    created: bool  # False: an equivalent job was already active and is returned instead


def retry_delay(attempt: int) -> timedelta:
    delay: timedelta = RETRY_BASE * (2 ** max(0, attempt - 1))
    return min(RETRY_MAX, delay)


def active_job(session: Session, kind: DiscoveryJobKind, provider: str) -> Job | None:
    return session.scalars(
        select(Job).where(
            Job.kind == kind, Job.provider == provider, Job.status.in_(ACTIVE_JOB_STATUSES)
        )
    ).one_or_none()


def create_job(
    session: Session,
    *,
    kind: DiscoveryJobKind,
    provider: str,
    trigger: JobTrigger,
    requested_by: str,
    params: dict[str, Any],
    now: datetime,
    max_attempts: int,
    parent_job_id: uuid.UUID | None = None,
) -> CreatedJob:
    """Create a PENDING job, or return the active job of the same kind."""
    existing = active_job(session, kind, provider)
    if existing is not None:
        return CreatedJob(existing, created=False)
    job = Job(
        id=uuid.uuid4(),
        kind=kind,
        provider=provider,
        status=DiscoveryJobStatus.PENDING,
        trigger=trigger,
        requested_by=requested_by,
        params=params,
        parent_job_id=parent_job_id,
        attempt=0,
        max_attempts=max_attempts,
        next_attempt_at=now,
        created_at=now,
        counters={},
        summary={},
        recent_errors=[],
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # Another request created the job between our check and our insert.
        existing = active_job(session, kind, provider)
        if existing is None:
            raise
        return CreatedJob(existing, created=False)
    return CreatedJob(job, created=True)


def claim_next(
    session: Session,
    *,
    owner: str,
    now: datetime,
    lease: timedelta,
    kinds: Sequence[DiscoveryJobKind] | None = None,
    job_id: uuid.UUID | None = None,
) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.status == DiscoveryJobStatus.PENDING, Job.next_attempt_at <= now)
        .order_by(Job.next_attempt_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(list(kinds)))
    if job_id is not None:
        stmt = stmt.where(Job.id == job_id)
    job = session.scalars(stmt).one_or_none()
    if job is None:
        return None
    job.status = DiscoveryJobStatus.RUNNING
    job.attempt += 1
    job.lease_owner = owner
    job.lease_expires_at = now + lease
    job.heartbeat_at = now
    job.started_at = job.started_at or now
    session.flush()
    return job


def _owned(session: Session, job_id: uuid.UUID, owner: str) -> Job:
    job = session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise JobStateError(f"discovery job {job_id} does not exist")
    if job.status is not DiscoveryJobStatus.RUNNING or job.lease_owner != owner:
        raise JobStateError(f"discovery job {job_id} is not running under {owner}")
    return job


def assert_owned(session: Session, job_id: uuid.UUID, owner: str) -> None:
    """Raise ``JobStateError`` unless ``owner`` still holds the job (row locked until the
    transaction ends): a runner that lost its lease must not commit results."""
    _owned(session, job_id, owner)


def heartbeat(
    session: Session,
    job_id: uuid.UUID,
    *,
    owner: str,
    now: datetime,
    lease: timedelta,
    counters: dict[str, Any] | None = None,
) -> None:
    job = _owned(session, job_id, owner)
    job.heartbeat_at = now
    job.lease_expires_at = now + lease
    if counters is not None:
        job.counters = dict(counters)


def finish(
    session: Session,
    job_id: uuid.UUID,
    *,
    owner: str,
    now: datetime,
    status: DiscoveryJobStatus,
    counters: dict[str, Any],
    summary: dict[str, Any],
    error: str | None = None,
) -> Job:
    if status not in FINISHED:
        raise JobStateError(f"{status.value} is not a final status")
    job = _owned(session, job_id, owner)
    job.status = status
    job.counters = dict(counters)
    job.summary = dict(summary)
    job.error = None if error is None else redact_text(error)
    job.completed_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = now
    return job


def _record_error(job: Job, now: datetime, code: str, message: str) -> None:
    entry = {"at": now.isoformat(), "attempt": job.attempt, "code": code, "message": message}
    job.recent_errors = [*(job.recent_errors or []), entry][-MAX_RECENT_ERRORS:]


def fail_attempt(
    session: Session,
    job_id: uuid.UUID,
    *,
    owner: str,
    now: datetime,
    code: str,
    message: str,
    permanent: bool,
    counters: dict[str, Any] | None = None,
) -> Job:
    """Record a failed attempt: re-queue with backoff, or FAILED when final."""
    job = _owned(session, job_id, owner)
    message = redact_text(message)[:500]
    _record_error(job, now, code, message)
    job.error = message
    if counters is not None:
        job.counters = dict(counters)
    job.lease_owner = None
    job.lease_expires_at = None
    if permanent or job.attempt >= job.max_attempts:
        job.status = DiscoveryJobStatus.FAILED
        job.completed_at = now
    else:
        job.status = DiscoveryJobStatus.PENDING
        job.next_attempt_at = now + retry_delay(job.attempt)
    return job


def recover_expired(session: Session, *, now: datetime) -> list[Job]:
    """Return abandoned RUNNING jobs (expired lease) to PENDING, or FAIL them."""
    stale = list(
        session.scalars(
            select(Job)
            .where(Job.status == DiscoveryJobStatus.RUNNING, Job.lease_expires_at < now)
            .with_for_update(skip_locked=True)
        )
    )
    for job in stale:
        _record_error(
            job, now, "worker_lost", f"lease held by {job.lease_owner} expired; job interrupted"
        )
        job.lease_owner = None
        job.lease_expires_at = None
        if job.attempt >= job.max_attempts:
            job.status = DiscoveryJobStatus.FAILED
            job.completed_at = now
            job.error = "interrupted repeatedly; attempts exhausted"
        else:
            job.status = DiscoveryJobStatus.PENDING
            job.next_attempt_at = now
    return stale


def last_finished(
    session: Session, kind: DiscoveryJobKind, provider: str, *, successful_only: bool = False
) -> Job | None:
    statuses = (
        [DiscoveryJobStatus.COMPLETED, DiscoveryJobStatus.PARTIALLY_COMPLETED]
        if successful_only
        else list(FINISHED)
    )
    return session.scalars(
        select(Job)
        .where(Job.kind == kind, Job.provider == provider, Job.status.in_(statuses))
        .order_by(Job.completed_at.desc())
        .limit(1)
    ).one_or_none()


__all__ = [
    "CreatedJob",
    "JobStateError",
    "active_job",
    "assert_owned",
    "claim_next",
    "create_job",
    "fail_attempt",
    "finish",
    "heartbeat",
    "last_finished",
    "recover_expired",
    "retry_delay",
]

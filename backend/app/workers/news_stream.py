"""Real-time news stream worker (Phase 3C): ``python -m app.workers.news_stream``.

One process owns the news connection of one provider (PostgreSQL advisory lock); the API
and every dashboard user read what it writes, so the provider sees a single connection.
It is independent of the market-data stream worker: its own process, endpoint, lock,
status row (``news_stream_status``) and sessions (``news_stream_sessions``).

Lifecycle::

    acquire lock -> disabled? record DISABLED and exit 0
    -> credentials missing? record FAILED and exit 2
    -> loop: connect -> authenticate -> subscribe ("*" or symbols; restored on every
             reconnect) -> recovery job over [interruption - overlap, now] in the
             background -> receive articles -> persist each frame (idempotent)
       on recoverable failure: record the interruption, bounded backoff, reconnect
       on permanent failure (credentials, entitlement): FAILED, no automatic retry
    -> SIGTERM/SIGINT: close the socket, wait briefly for the recovery job, record STOPPED

Persistence is inline: while PostgreSQL is unavailable the worker retries the frame in
hand and stops reading, so the provider eventually drops the connection and the reconnect
runs a recovery retrieval for the whole interval. Nothing is dropped silently: frames that
cannot be written by shutdown are counted, and the next start recovers from the last
persisted point (D-080).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import random
import signal
import socket
import sys
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.clocks import Clock, SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.logging import configure_logging, get_logger
from app.core.security.redaction import redact_text
from app.data_engine.enums import StreamAuthState, StreamSubscriptionState
from app.data_engine.errors import MarketDataError, StreamError
from app.data_engine.schemas import ConnectionState, CredentialStatus
from app.database.models import NewsStreamSessionRecord, NewsStreamStatusRecord
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.news.enums import NewsJobKind
from app.news.runtime import NewsContext, build_news_context
from app.news.schemas import NewsStreamBatch
from app.news.store import IngestResult

logger = get_logger(__name__)

T = TypeVar("T")

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_ALREADY_RUNNING = 3
EXIT_STREAM_FAILED = 4

MAX_RECENT_ERRORS = 20
INTERRUPTED_REASON = "interrupted: the worker stopped without closing this session"
REQUESTED_BY = "news_stream_worker"

COUNTERS = (
    "frames_received",
    "articles_received",
    "articles_new",
    "revisions_new",
    "duplicates",
    "malformed",
    "frames_unpersisted",
    "persist_failures",
    "reconnects",
    "recovery_jobs",
    "recovery_jobs_failed",
    "recovery_articles_new",
)


class NewsStreamWorker:
    def __init__(
        self,
        *,
        context: NewsContext,
        clock: Clock | None = None,
        worker_id: str | None = None,
        backoff_base: float = 1.0,
        persist_retry_max: float = 30.0,
        recovery_wait: float = 30.0,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._context = context
        self._sessions = context.session_factory
        self._provider = context.provider
        self._settings = context.settings
        self._service = context.service
        self._clock = clock or context.clock or SystemClock()
        self.worker_id = worker_id or (
            f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._backoff_base = backoff_base
        self._persist_retry_max = persist_retry_max
        self._recovery_wait = recovery_wait
        self._jitter = jitter or (lambda: random.uniform(0.8, 1.2))  # noqa: S311  # not crypto
        self._lock_connection: Connection | None = None
        self._recovery_lock = asyncio.Lock()
        self._recoveries: set[asyncio.Task[None]] = set()

        self.desired: frozenset[str] = frozenset(self._settings.stream_symbols)
        self.confirmed: frozenset[str] = frozenset()
        self.state = ConnectionState.DISCONNECTED
        self.auth_state = StreamAuthState.NOT_ATTEMPTED
        self.subscription_state = StreamSubscriptionState.NONE
        self.session_id: uuid.UUID | None = None
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.last_authenticated_at: datetime | None = None
        self.last_subscribed_at: datetime | None = None
        self.last_message_at: datetime | None = None
        self.last_article_at: datetime | None = None
        self.last_persisted_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_ERRORS)
        self.reconnect_count = 0
        self.counters: Counter[str] = Counter()
        self._session_counters: Counter[str] = Counter()
        self._session_first_message_at: datetime | None = None
        self._session_last_article_at: datetime | None = None
        self._session_authenticated_at: datetime | None = None
        self._session_subscribed_at: datetime | None = None
        # Start of the interval whose articles may be missing (recovered after subscribing).
        self._interrupted_since: datetime | None = None
        self._failed = False
        self.current_stream: Any = None  # observability and fault injection in tests

    # ------------------------------------------------------------ entry point
    async def run(self, stop: asyncio.Event) -> int:
        self.started_at = self._clock.now()
        if not self._acquire_lock():
            logger.error("news_stream.already_running", provider=self._provider.provider_id)
            return EXIT_ALREADY_RUNNING
        try:
            return await self._run_locked(stop)
        finally:
            self._release_lock()

    async def _run_locked(self, stop: asyncio.Event) -> int:
        if not self._settings.stream_enabled:
            self.state = ConnectionState.DISABLED
            await self._write_status(enabled=False)
            logger.info("news_stream.disabled", reason="NEWS_STREAM_ENABLED is false")
            return EXIT_OK
        if self._provider.credential_status() is CredentialStatus.NOT_CONFIGURED:
            self.state = ConnectionState.FAILED
            self._error("configuration", "provider credentials are not configured")
            await self._write_status()
            return EXIT_CONFIGURATION
        if not self._provider.capabilities().streaming:
            self.state = ConnectionState.FAILED
            self._error("configuration", "the news provider offers no real-time stream")
            await self._write_status()
            return EXIT_CONFIGURATION

        self._interrupted_since = await asyncio.to_thread(self._previous_run)
        logger.info(
            "news_stream.starting",
            provider=self._provider.provider_id,
            symbols=sorted(self.desired)[:50],
            worker_id=self.worker_id,
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop(stop), name="news-heartbeat")
        try:
            await self._connection_loop(stop)
        finally:
            stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._finish_recoveries()
            self.state = ConnectionState.FAILED if self._failed else ConnectionState.STOPPED
            self.stopped_at = self._clock.now()
            await self._write_status()
            logger.info("news_stream.stopped", state=self.state.value, counters=dict(self.counters))
        return EXIT_STREAM_FAILED if self._failed else EXIT_OK

    # ------------------------------------------------------------ connection lifecycle
    async def _connection_loop(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            attempt += 1
            try:
                stream = self._provider.create_stream()
            except MarketDataError as exc:
                self._fail(f"cannot create stream: {exc.message}")
                break
            self.current_stream = stream
            self.session_id = await asyncio.to_thread(self._open_session, attempt, stream.url)
            self._session_counters = Counter()
            self._session_first_message_at = None
            self._session_last_article_at = None
            self._session_authenticated_at = None
            self._session_subscribed_at = None
            end_reason = "closed"
            try:
                self.state = ConnectionState.CONNECTING
                self.auth_state = StreamAuthState.PENDING
                self.subscription_state = StreamSubscriptionState.NONE
                await self._write_status()
                if not await self._until_stopped(stop, stream.connect()):
                    end_reason = "shutdown"
                    continue
                self.auth_state = stream.auth_state
                self.last_authenticated_at = self._clock.now()
                self._session_authenticated_at = self.last_authenticated_at
                self.state = ConnectionState.SUBSCRIBING
                self.subscription_state = StreamSubscriptionState.PENDING
                await self._write_status()
                if not await self._until_stopped(stop, stream.subscribe(self.desired)):
                    end_reason = "shutdown"
                    continue
                self._after_subscribe(stream.confirmed_symbols, stream.subscription_state)
                attempt = 0
                await asyncio.to_thread(self._update_session)
                await self._write_status()
                self._schedule_recovery(self._session_subscribed_at or self._clock.now())
                completed = await self._until_stopped(stop, self._receive(stream))
                end_reason = "shutdown" if not completed or stop.is_set() else "closed by provider"
                if completed and not stop.is_set():
                    raise StreamError("news stream ended without an error")
            except StreamError as exc:
                if self._interrupted_since is None:
                    self._interrupted_since = self._clock.now()
                self.auth_state = (
                    StreamAuthState.REJECTED
                    if stream.auth_state is StreamAuthState.REJECTED
                    else StreamAuthState.NOT_ATTEMPTED
                )
                self.subscription_state = (
                    StreamSubscriptionState.REJECTED
                    if stream.subscription_state is StreamSubscriptionState.REJECTED
                    else StreamSubscriptionState.NONE
                )
                end_reason = exc.message
                if exc.permanent:
                    self._fail(exc.message)
                    break
                self._error("stream", exc.message, code=exc.code)
                self.state = ConnectionState.RECONNECTING
                self.reconnect_count += 1
                self.counters["reconnects"] += 1
                delay = self._backoff(attempt)
                logger.warning(
                    "news_stream.reconnecting",
                    reason=exc.message,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                )
                await self._write_status()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
            finally:
                await stream.close()
                await asyncio.to_thread(self._close_session, end_reason)

        if self._failed:
            await self._write_status()
            await stop.wait()  # report FAILED until stopped; never retry automatically

    async def _until_stopped(self, stop: asyncio.Event, work: Awaitable[T]) -> bool:
        task = asyncio.ensure_future(work)
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        if task in done:
            task.result()
            return True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, StreamError):
            await task
        return False

    def _after_subscribe(self, confirmed: frozenset[str], state: StreamSubscriptionState) -> None:
        self.confirmed = confirmed
        self.subscription_state = state
        self.state = ConnectionState.SUBSCRIBED
        self.last_subscribed_at = self._clock.now()
        self._session_subscribed_at = self.last_subscribed_at
        missing = self.desired - confirmed
        if missing:
            self._error("subscription", f"provider did not confirm: {sorted(missing)[:20]}")
        logger.info(
            "news_stream.subscribed",
            state=state.value,
            confirmed=sorted(confirmed)[:50],
            reconnect_count=self.reconnect_count,
        )

    async def _receive(self, stream: Any) -> None:
        async for batch in stream.events():
            await self._on_batch(batch, stream)

    async def _on_batch(self, batch: NewsStreamBatch, stream: Any) -> None:
        now = batch.received_at
        self.last_message_at = now
        if self._session_first_message_at is None:
            self._session_first_message_at = now
        for counter in (self.counters, self._session_counters):
            counter["frames_received"] += 1
            counter["articles_received"] += len(batch.articles)
            counter["malformed"] += len(batch.malformed)
        for item in batch.malformed:
            self._error("malformed", f"article {item.provider_article_id}: {item.reason}")
        for notice in batch.notices:
            if notice.get("kind") == "control:error":
                self._error("provider", f"provider error on stream: {notice.get('details')}")
            elif notice.get("kind") == "control:subscription":
                self.confirmed = stream.confirmed_symbols
        if batch.articles:
            self.last_article_at = now
            self._session_last_article_at = now
            await self._persist(batch)

    async def _persist(self, batch: NewsStreamBatch) -> None:
        """Write one frame, retrying database failures until it is written or the worker
        stops (cancellation). While retrying, nothing more is read from the socket."""
        attempt = 0
        session_id = self.session_id
        while True:
            try:
                result = await asyncio.to_thread(self._persist_sync, batch, session_id)
                break
            except SQLAlchemyError as exc:
                attempt += 1
                self.counters["persist_failures"] += 1
                if self._interrupted_since is None or batch.received_at < self._interrupted_since:
                    # The recovery after the next (re)connection starts no later than this.
                    self._interrupted_since = batch.received_at
                self._error("database", f"write failed: {type(exc).__name__}")
                logger.warning(
                    "news_stream.persist_failed", error_type=type(exc).__name__, attempt=attempt
                )
                try:
                    await asyncio.sleep(min(self._persist_retry_max, 0.5 * 2 ** min(attempt, 10)))
                except asyncio.CancelledError:
                    self.counters["frames_unpersisted"] += 1
                    raise
        if attempt:
            logger.info("news_stream.persist_recovered", attempts=attempt + 1)
        self.last_persisted_at = self._clock.now()
        for counter in (self.counters, self._session_counters):
            counter["articles_new"] += result.new_articles
            counter["revisions_new"] += result.new_revisions
            counter["duplicates"] += result.duplicates

    def _persist_sync(self, batch: NewsStreamBatch, session_id: uuid.UUID | None) -> IngestResult:
        with session_scope(self._sessions) as session:
            return self._service.store(session).ingest(batch.articles, stream_session_id=session_id)

    # ------------------------------------------------------------ recovery (D-080)
    def _schedule_recovery(self, subscribed_at: datetime) -> None:
        since = self._interrupted_since
        self._interrupted_since = None
        overlap = timedelta(seconds=self._settings.stream_recovery_overlap_seconds)
        start = (since or subscribed_at) - overlap
        end = self._clock.now()
        earliest = end - timedelta(hours=self._settings.stream_recovery_max_hours)
        capped = start < earliest
        start = max(start, earliest)
        if end - start < timedelta(seconds=1):
            return
        task = asyncio.create_task(
            self._recover(start, end, capped, self.session_id), name="news-recovery"
        )
        self._recoveries.add(task)
        task.add_done_callback(self._recoveries.discard)

    async def _recover(
        self, start: datetime, end: datetime, capped: bool, session_id: uuid.UUID | None
    ) -> None:
        async with self._recovery_lock:  # one recovery retrieval at a time
            symbols = () if self._settings.stream_all_news else tuple(sorted(self.desired))
            try:
                job = await asyncio.to_thread(
                    lambda: self._service.create_job(
                        kind=NewsJobKind.STREAM_RECOVERY,
                        start=start,
                        end=end,
                        symbols=symbols,
                        requested_by=REQUESTED_BY,
                        stream_session_id=session_id,
                        params={
                            "overlap_seconds": self._settings.stream_recovery_overlap_seconds,
                            "capped_at_max_hours": capped,
                        },
                    )
                )
                self.counters["recovery_jobs"] += 1
                finished = await self._service.run_job(job.id)
            except Exception as exc:
                self.counters["recovery_jobs_failed"] += 1
                self._error("recovery", f"recovery retrieval failed: {type(exc).__name__}")
                logger.exception("news_stream.recovery_failed")
                return
            if finished.status.value != "COMPLETED":
                self.counters["recovery_jobs_failed"] += 1
                self._error("recovery", f"recovery job {finished.id}: {finished.error}")
            self.counters["recovery_articles_new"] += finished.articles_new
            if capped:
                self._error(
                    "recovery",
                    "interruption longer than NEWS_STREAM_RECOVERY_MAX_HOURS; earlier articles "
                    "need a manual backfill",
                )
            logger.info(
                "news_stream.recovered",
                job_id=str(finished.id),
                status=finished.status.value,
                new_articles=finished.articles_new,
                duplicates=finished.duplicates,
            )

    async def _finish_recoveries(self) -> None:
        if not self._recoveries:
            return
        _, pending = await asyncio.wait(set(self._recoveries), timeout=self._recovery_wait)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _previous_run(self) -> datetime | None:
        """Where the recovery after this start must begin, from the previous worker's record;
        then close the sessions a crashed worker left open.

        * Graceful stop: the previous session's end (earlier only if frames received before
          it were never persisted, e.g. shutdown during a database outage).
        * Crash (session never closed): the last heartbeat, or the last frame received if it
          was never persisted (persistence is inline, so at most that frame was in hand).
        * No previous session: ``None`` (the recovery overlap covers connecting).
        """
        provider = self._provider.provider_id
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            previous = session.scalars(
                select(NewsStreamSessionRecord)
                .where(NewsStreamSessionRecord.provider == provider)
                .order_by(NewsStreamSessionRecord.started_at.desc())
                .limit(1)
            ).one_or_none()
            status = session.get(NewsStreamStatusRecord, provider)
            since: datetime | None = None
            if previous is not None:
                last_message = status.last_message_at if status is not None else None
                last_persisted = status.last_persisted_at if status is not None else None
                if previous.ended_at is not None:
                    since = previous.ended_at
                    unpersisted = status is not None and bool(
                        (status.counters or {}).get("frames_unpersisted")
                    )
                    if unpersisted and last_message is not None:
                        since = min(since, last_message)
                else:
                    since = status.heartbeat_at if status is not None else None
                    in_flight = last_message is not None and (
                        last_persisted is None or last_message > last_persisted
                    )
                    if in_flight and last_message is not None:
                        since = min(since or last_message, last_message)
                    if since is None:
                        since = previous.last_message_at or previous.started_at
            session.execute(
                update(NewsStreamSessionRecord)
                .where(
                    NewsStreamSessionRecord.provider == provider,
                    NewsStreamSessionRecord.ended_at.is_(None),
                )
                .values(ended_at=now, end_reason=INTERRUPTED_REASON, state="STOPPED")
            )
            return since

    # ------------------------------------------------------------ status and sessions
    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._write_status()
            if self.session_id is not None:
                with contextlib.suppress(SQLAlchemyError):
                    await asyncio.to_thread(self._update_session)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.stream_status_interval)

    def snapshot(self, *, enabled: bool = True) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "enabled": enabled,
            "connection_state": self.state.value,
            "auth_state": self.auth_state.value,
            "subscription_state": self.subscription_state.value,
            "current_session_id": self.session_id,
            "requested_symbols": sorted(self.desired),
            "confirmed_symbols": sorted(self.confirmed),
            "worker_started_at": self.started_at,
            "worker_stopped_at": self.stopped_at,
            "heartbeat_at": self._clock.now(),
            "last_authenticated_at": self.last_authenticated_at,
            "last_subscribed_at": self.last_subscribed_at,
            "last_message_at": self.last_message_at,
            "last_article_at": self.last_article_at,
            "last_persisted_at": self.last_persisted_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "recent_errors": list(self.recent_errors),
            "reconnect_count": self.reconnect_count,
            "counters": {name: self.counters.get(name, 0) for name in COUNTERS},
        }

    async def _write_status(self, *, enabled: bool = True) -> None:
        try:
            await asyncio.to_thread(self._status_write, enabled)
        except SQLAlchemyError as exc:
            logger.warning("news_stream.status_write_failed", error_type=type(exc).__name__)

    def _status_write(self, enabled: bool) -> None:
        values = self.snapshot(enabled=enabled)
        provider = self._provider.provider_id
        with session_scope(self._sessions) as session:
            stmt = insert(NewsStreamStatusRecord).values(provider=provider, **values)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["provider"], set_={**values, "updated_at": func.now()}
                )
            )

    def _open_session(self, attempt: int, url: str) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            record = NewsStreamSessionRecord(
                id=uuid.uuid4(),
                provider=self._provider.provider_id,
                worker_id=self.worker_id,
                attempt=attempt,
                state=ConnectionState.CONNECTING.value,
                url=url[:256],
                requested_symbols=sorted(self.desired),
                started_at=self._clock.now(),
            )
            session.add(record)
            session.flush()
            return record.id

    def _session_values(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "confirmed_symbols": sorted(self.confirmed),
            "authenticated_at": self._session_authenticated_at,
            "subscribed_at": self._session_subscribed_at,
            "first_message_at": self._session_first_message_at,
            "last_message_at": self.last_message_at
            if self._session_first_message_at is not None
            else None,
            "last_article_at": self._session_last_article_at,
            "counters": dict(self._session_counters),
        }

    def _update_session(self) -> None:
        if self.session_id is None:
            return
        values = {k: v for k, v in self._session_values().items() if v is not None}
        with session_scope(self._sessions) as session:
            session.execute(
                update(NewsStreamSessionRecord)
                .where(NewsStreamSessionRecord.id == self.session_id)
                .values(**values)
            )

    def _close_session(self, reason: str) -> None:
        if self.session_id is None:
            return
        values = {k: v for k, v in self._session_values().items() if v is not None}
        values.update(ended_at=self._clock.now(), end_reason=redact_text(reason)[:500])
        try:
            with session_scope(self._sessions) as session:
                session.execute(
                    update(NewsStreamSessionRecord)
                    .where(NewsStreamSessionRecord.id == self.session_id)
                    .values(**values)
                )
        except SQLAlchemyError as exc:
            logger.warning("news_stream.session_close_failed", error_type=type(exc).__name__)

    def _fail(self, message: str) -> None:
        self._failed = True
        self.state = ConnectionState.FAILED
        self._error("permanent", message)
        logger.critical("news_stream.failed_permanently", detail=redact_text(message))

    def _error(self, kind: str, message: str, code: str | None = None) -> None:
        message = redact_text(message)[:500]
        at = self._clock.now()
        self.last_error, self.last_error_at = message, at
        self.recent_errors.append(
            {"at": at.isoformat(), "kind": kind, "code": code, "message": message}
        )

    def _backoff(self, attempt: int) -> float:
        cap = self._settings.stream_reconnect_max_delay
        delay = min(cap, self._backoff_base * 2 ** max(0, attempt - 1))
        return float(min(cap, max(0.0, delay * self._jitter())))

    # ------------------------------------------------------------ single-worker lock
    def _lock_key(self) -> str:
        return f"news_stream:{self._provider.provider_id}"

    def _acquire_lock(self) -> bool:
        bind = self._sessions.kw.get("bind")
        if bind is None:
            raise RuntimeError("session factory has no engine bound")
        connection: Connection = bind.connect()
        acquired = connection.execute(
            select(func.pg_try_advisory_lock(func.hashtextextended(self._lock_key(), 0)))
        ).scalar_one()
        connection.commit()
        if not acquired:
            connection.close()
            return False
        self._lock_connection = connection
        return True

    def _release_lock(self) -> None:
        connection, self._lock_connection = self._lock_connection, None
        if connection is None:
            return
        try:
            connection.execute(
                select(func.pg_advisory_unlock(func.hashtextextended(self._lock_key(), 0)))
            )
            connection.commit()
        except SQLAlchemyError:
            logger.warning("news_stream.lock_release_failed")
        finally:
            connection.close()


# ------------------------------------------------------------------ process entry point
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _main_async(duration: float | None) -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=f"{settings.service_name}-news-stream",
        environment=settings.environment.value,
    )
    clock = SystemClock()
    engine = build_engine(settings)
    context = build_news_context(build_session_factory(engine), clock)
    worker = NewsStreamWorker(context=context, clock=clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)
    if duration is not None:
        loop.call_later(duration, stop.set)
    try:
        return await worker.run(stop)
    finally:
        await context.provider.aclose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.news_stream",
        description="Run the real-time news stream worker (news only; no orders).",
    )
    parser.add_argument(
        "--duration", type=float, help="stop after this many seconds (default: run until signalled)"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args.duration))
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError, MarketDataError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_CONFIGURATION


__all__ = [
    "EXIT_ALREADY_RUNNING",
    "EXIT_CONFIGURATION",
    "EXIT_OK",
    "EXIT_STREAM_FAILED",
    "NewsStreamWorker",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())

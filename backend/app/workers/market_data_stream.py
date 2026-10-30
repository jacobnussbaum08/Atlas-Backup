"""Live market-data stream worker (Deliverable 2C): ``python -m app.workers.market_data_stream``.

One process owns one provider/feed connection. It runs outside FastAPI, so API requests
never open or wait on a stream; the API reads the state this worker writes to PostgreSQL.

Lifecycle::

    acquire single-worker lock (PostgreSQL advisory lock per provider/feed)
    -> disabled? record DISABLED and exit 0
    -> validate configuration and channels, resolve assets/mappings
    -> record the interval since the previous worker as a gap
    -> loop: connect -> authenticate -> subscribe (full desired set, i.e. restored after
             every reconnect) -> receive frames -> bounded queue -> batched writes
       on recoverable failure: record gap, bounded exponential backoff, reconnect
       on permanent failure (credentials, entitlement, symbol limit): FAILED, no retry
    -> on SIGTERM/SIGINT: close the socket, drain the queue, record STOPPED, release lock

Nothing received is dropped silently: if the queue stays full the frame is discarded
*and* the loss is recorded as a ``DATA_DROPPED`` gap plus quality event, and health
reports DEGRADED.

Deliverable 2D:
* Frames carry their 1-based ``frame_seq`` per session; a group retried after a lost
  commit acknowledgement is recognised and not written twice.
* While subscribed and keeping up, the worker extends **live coverage evidence** per
  asset and channel (``market_data_coverage``) up to the newest moment from which every
  received frame is persisted. A drop freezes the segment; a new one starts after it.
  For bars this is completeness evidence; for trades/quotes it only marks the interval
  for reconciliation against the provider's history.
* At start-up, frames the previous worker received but never persisted (it died with
  them in memory) are recorded as a ``DATA_NOT_PERSISTED`` interval for recovery.
* Database interruptions are counted, timed and audited when writes succeed again.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import random
import signal
import socket
import statistics
import sys
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import Connection, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock, SystemClock
from app.core.config import (
    InvalidSettingsError,
    MarketDataSettings,
    UnknownSettingError,
    describe_validation_error,
    get_market_data_settings,
    get_settings,
)
from app.core.logging import configure_logging, get_logger
from app.core.security.redaction import redact_text
from app.data_engine.completeness import live_bar_coverage
from app.data_engine.enums import (
    CoverageEvidence,
    DataQualityEventType,
    DataType,
    Severity,
    StreamAuthState,
    StreamChannel,
    StreamGapKind,
    StreamSubscriptionState,
)
from app.data_engine.errors import MarketDataError, StreamError
from app.data_engine.live import COUNTER_NAMES, LiveIngestionService, UnknownStreamAssetError
from app.data_engine.providers.base import MarketDataProvider, StreamBatch, StreamSubscription
from app.data_engine.providers.registry import create_provider
from app.data_engine.schemas import ConnectionState
from app.data_engine.timeframes import Timeframe
from app.data_engine.validation import QualityIssue
from app.database.models import ActorType, RawMarketDataEventRecord, StreamSessionRecord
from app.database.repositories import (
    AuditLogRepository,
    CompletenessRepository,
    Series,
    StreamRepository,
)
from app.database.sessions import build_engine, build_session_factory, session_scope

logger = get_logger(__name__)

T = TypeVar("T")

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_ALREADY_RUNNING = 3
EXIT_STREAM_FAILED = 4

MAX_RECENT_ERRORS = 20
INTERRUPTED_REASON = "interrupted: the worker stopped without closing this session"


@dataclass
class _Drops:
    frames: int = 0
    items: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None


@dataclass
class _Latency:
    """Rolling latency samples in seconds."""

    samples: deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    def add(self, values: list[float]) -> None:
        self.samples.extend(values)

    def summary(self) -> dict[str, float | int] | None:
        if not self.samples:
            return None
        values = sorted(self.samples)
        return {
            "count": len(values),
            "mean": round(statistics.fmean(values), 6),
            "p50": round(values[len(values) // 2], 6),
            "p95": round(values[min(len(values) - 1, int(len(values) * 0.95))], 6),
            "max": round(values[-1], 6),
        }


class StreamWorker:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        settings: MarketDataSettings,
        clock: Clock | None = None,
        worker_id: str | None = None,
        heartbeat_interval: float = 5.0,
        backoff_base: float = 1.0,
        flush_max_frames: int = 500,
        flush_interval: float = 0.2,
        enqueue_timeout: float = 2.0,
        drain_timeout: float = 15.0,
        persist_retry_max: float = 30.0,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock or SystemClock()
        self.worker_id = worker_id or (
            f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._heartbeat_interval = heartbeat_interval
        self._backoff_base = backoff_base
        self._flush_max_frames = flush_max_frames
        self._flush_interval = flush_interval
        self._enqueue_timeout = enqueue_timeout
        self._drain_timeout = drain_timeout
        self._persist_retry_max = persist_retry_max
        self._jitter = jitter or (lambda: random.uniform(0.8, 1.2))  # noqa: S311  # not crypto
        self._live = LiveIngestionService(
            session_factory,
            provider=provider.provider_id,
            feed=provider.feed,
            clock=self._clock,
            stale_after=(
                timedelta(seconds=settings.market_data_stale_observation_seconds)
                if settings.market_data_stale_observation_seconds > 0
                else None
            ),
        )
        self._queue: asyncio.Queue[tuple[uuid.UUID, int, StreamBatch]] = asyncio.Queue(
            maxsize=settings.market_data_stream_queue_size
        )
        self._lock_connection: Connection | None = None

        # Observable state (written to market_data_stream_status).
        self.state = ConnectionState.DISCONNECTED
        self.auth_state = StreamAuthState.NOT_ATTEMPTED
        self.subscription_state = StreamSubscriptionState.NONE
        self.desired = StreamSubscription()
        self.confirmed = StreamSubscription()
        self.session_id: uuid.UUID | None = None
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.last_authenticated_at: datetime | None = None
        self.last_subscribed_at: datetime | None = None
        self.last_message_at: datetime | None = None
        self.last_persisted_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_ERRORS)
        self.reconnect_count = 0
        self.counters: Counter[str] = Counter()
        self.persistence_failing = False
        self._drops = _Drops()
        self._last_drop_at: datetime | None = None
        self._commit_latency = _Latency()
        self._event_lag = _Latency()
        self._persist_seconds = _Latency()
        self._session_counters: Counter[str] = Counter()
        self._session_first_message_at: datetime | None = None
        self._session_authenticated_at: datetime | None = None
        self._session_subscribed_at: datetime | None = None
        self._failed = False
        # 2D: persistence progress and live coverage evidence.
        self.last_bar_at: datetime | None = None  # newest completed bar persisted
        self.persisting_since: datetime | None = None  # database failing since
        self._persisted_through: datetime | None = None  # newest frame committed (received_at)
        self._group_in_flight = False
        self._segment_start: datetime | None = None
        self._segment_ids: dict[tuple[uuid.UUID, DataType], int] = {}
        self._drops_since_heartbeat = False
        # The live connection object (observability and fault-injection in live tests).
        self.current_stream: Any = None

    # ------------------------------------------------------------ entry point
    async def run(self, stop: asyncio.Event) -> int:
        provider, feed = self._provider.provider_id, self._provider.feed
        self.started_at = self._clock.now()
        if not self._acquire_lock():
            logger.error("stream.already_running", provider=provider, feed=feed)
            return EXIT_ALREADY_RUNNING
        try:
            return await self._run_locked(stop)
        finally:
            self._release_lock()

    async def _run_locked(self, stop: asyncio.Event) -> int:
        settings = self._settings
        if not settings.market_data_stream_enabled:
            self.state = ConnectionState.DISABLED
            await self._write_status(enabled=False)
            logger.info("stream.disabled", reason="MARKET_DATA_STREAM_ENABLED is false")
            return EXIT_OK

        problem = self._configuration_problem()
        if problem is None:
            try:
                targets = await asyncio.to_thread(
                    self._live.load_targets, settings.market_data_symbols
                )
            except (UnknownStreamAssetError, MarketDataError) as exc:
                problem = str(exc)
            else:
                channels = frozenset(settings.market_data_stream_channels)
                self.desired = StreamSubscription.of(channels, frozenset(targets))
        if problem is not None:
            self.state = ConnectionState.FAILED
            self._error("configuration", problem)
            await self._write_status()
            logger.error("stream.configuration_invalid", detail=problem)
            return EXIT_CONFIGURATION

        await asyncio.to_thread(self._record_previous_run)
        logger.info(
            "stream.starting",
            provider=self._provider.provider_id,
            feed=self._provider.feed,
            subscription=self.desired.as_dict(),
            worker_id=self.worker_id,
        )
        persister = asyncio.create_task(self._persist_loop(stop), name="stream-persister")
        heartbeat = asyncio.create_task(self._heartbeat_loop(stop), name="stream-heartbeat")
        try:
            await self._connection_loop(stop)
        finally:
            stop.set()
            await self._finish_task(persister, self._drain_timeout + 5)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.state = ConnectionState.FAILED if self._failed else ConnectionState.STOPPED
            self.stopped_at = self._clock.now()
            await self._flush_drops()
            await self._write_status()
            logger.info("stream.stopped", state=self.state.value, counters=dict(self.counters))
        return EXIT_STREAM_FAILED if self._failed else EXIT_OK

    def _configuration_problem(self) -> str | None:
        report = self._provider.validate_configuration(self._settings)
        if not report.ok:
            return "; ".join(f"{i.code}: {i.message}" for i in report.errors)
        requested = frozenset(self._settings.market_data_stream_channels)
        unsupported = requested - self._provider.streaming_channels()
        if unsupported:
            names = ", ".join(sorted(c.value for c in unsupported))
            return (
                f"channel(s) {names} are not available from {self._provider.provider_id} "
                f"feed '{self._provider.feed}' on this subscription"
            )
        return None

    # ------------------------------------------------------------ connection lifecycle
    async def _connection_loop(self, stop: asyncio.Event) -> None:
        attempt = 0
        streaming = False  # True once data could flow (subscription confirmed)
        while not stop.is_set():
            attempt += 1
            try:
                stream = self._provider.create_stream()
                self.current_stream = stream
            except MarketDataError as exc:
                self._fail(f"cannot create stream: {exc.message}")
                break
            self.session_id = await asyncio.to_thread(self._open_session, attempt, stream)
            self._session_counters = Counter()
            self._session_first_message_at = None
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
                self._after_subscribe(stream.subscription, stream.subscription_state)
                streaming = True
                attempt = 0
                self._session_subscribed_at = self.last_subscribed_at
                self._segment_start, self._segment_ids = self._session_subscribed_at, {}
                await asyncio.to_thread(self._update_session)
                await asyncio.to_thread(self._recovered)
                await self._write_status()
                completed = await self._until_stopped(stop, self._receive(stream))
                end_reason = "shutdown" if not completed or stop.is_set() else "closed by provider"
                if completed and not stop.is_set():
                    raise StreamError("stream ended without an error")
            except StreamError as exc:
                self._segment_start, self._segment_ids = None, {}
                # A lost connection is no longer authenticated or subscribed.
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
                if streaming:
                    await asyncio.to_thread(
                        self._open_gap, StreamGapKind.CONNECTION_LOST, exc.message
                    )
                    streaming = False
                self.state = ConnectionState.RECONNECTING
                self.reconnect_count += 1
                self.counters["reconnects"] += 1
                delay = self._backoff(attempt)
                logger.warning(
                    "stream.reconnecting",
                    reason=exc.message,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                    reconnect_count=self.reconnect_count,
                )
                await self._write_status()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
            finally:
                await stream.close()
                await asyncio.to_thread(self._close_session, end_reason)

        if self._failed:
            # Permanent failure: report FAILED until stopped; never retry automatically.
            await self._write_status()
            await stop.wait()

    async def _until_stopped(self, stop: asyncio.Event, work: Awaitable[T]) -> bool:
        """Run ``work`` unless ``stop`` is set first. True if ``work`` completed."""
        task = asyncio.ensure_future(work)
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        if task in done:
            task.result()  # re-raise the work's exception
            return True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, StreamError):
            await task
        return False

    async def _receive(self, stream: Any) -> None:
        async for batch in stream.events():
            self._on_batch(batch, stream)
            await self._enqueue(batch)

    def _on_batch(self, batch: StreamBatch, stream: Any) -> None:
        """Bookkeeping on receipt. The session's ``frames_received`` is the frame's seq."""
        now = batch.raw_event.received_at
        self.last_message_at = now
        if self._session_first_message_at is None:
            self._session_first_message_at = now
        for counter in (self.counters, self._session_counters):
            counter["frames_received"] += 1
            counter["items_received"] += batch.item_count
        for notice in batch.notices:
            if notice.kind == "control:error":
                self._error("provider", f"provider error on stream: {notice.details}")
            elif notice.kind == "control:subscription":
                self.confirmed = stream.subscription

    async def _enqueue(self, batch: StreamBatch) -> None:
        assert self.session_id is not None  # noqa: S101  # set before receiving
        item = (self.session_id, self._session_counters["frames_received"], batch)
        try:
            self._queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self._queue.put(item), timeout=self._enqueue_timeout)
        except TimeoutError:
            drops = self._drops
            drops.frames += 1
            drops.items += batch.item_count
            at = batch.raw_event.received_at
            drops.first_at = drops.first_at or at
            drops.last_at = at
            self._last_drop_at = at
            self._break_coverage()
            self.counters["frames_dropped"] += 1
            self.counters["items_dropped"] += batch.item_count
            if drops.frames == 1 or drops.frames % 100 == 0:
                logger.error(
                    "stream.frames_dropped",
                    frames=drops.frames,
                    queue_capacity=self._queue.maxsize,
                    reason="persistence is not keeping up",
                )

    def _after_subscribe(
        self, confirmed: StreamSubscription, state: StreamSubscriptionState
    ) -> None:
        self.confirmed = confirmed
        self.subscription_state = state
        self.state = ConnectionState.SUBSCRIBED
        self.last_subscribed_at = self._clock.now()
        missing = self.desired.missing_from(confirmed)
        if missing:
            detail = {c.value: sorted(s) for c, s in missing.items()}
            self._error("subscription", f"provider did not confirm: {detail}")
        logger.info(
            "stream.subscribed",
            state=state.value,
            confirmed=confirmed.as_dict(),
            reconnect_count=self.reconnect_count,
        )

    def _fail(self, message: str) -> None:
        self._failed = True
        self.state = ConnectionState.FAILED
        self._error("permanent", message)
        logger.critical("stream.failed_permanently", detail=redact_text(message))

    def _error(self, kind: str, message: str, code: str | None = None) -> None:
        message = redact_text(message)[:500]
        at = self._clock.now()
        self.last_error, self.last_error_at = message, at
        self.recent_errors.append(
            {"at": at.isoformat(), "kind": kind, "code": code, "message": message}
        )

    def _backoff(self, attempt: int) -> float:
        cap = self._settings.market_data_reconnect_max_delay
        delay = min(cap, self._backoff_base * 2 ** max(0, attempt - 1))
        return float(min(cap, max(0.0, delay * self._jitter())))

    # ------------------------------------------------------------ persistence
    async def _persist_loop(self, stop: asyncio.Event) -> None:
        drain_deadline: float | None = None
        while True:
            if stop.is_set() and drain_deadline is None:
                drain_deadline = time.monotonic() + self._drain_timeout
            if stop.is_set() and self._queue.empty():
                return
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            group = [first]
            deadline = time.monotonic() + self._flush_interval
            while len(group) < self._flush_max_frames:
                try:
                    group.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or stop.is_set():
                        break
                    try:
                        group.append(await asyncio.wait_for(self._queue.get(), remaining))
                    except TimeoutError:
                        break
            for session_id in dict.fromkeys(sid for sid, _, _ in group):
                items = [(seq, b) for sid, seq, b in group if sid == session_id]
                if not await self._persist_group(session_id, items, drain_deadline, stop):
                    return

    async def _persist_group(
        self,
        session_id: uuid.UUID,
        items: list[tuple[int, StreamBatch]],
        drain_deadline: float | None,
        stop: asyncio.Event,
    ) -> bool:
        """Write one group, retrying database failures. False if shutdown abandoned it.

        A retry after a lost commit acknowledgement is safe: the group's first frame
        sequence is found stored and nothing is written twice.
        """
        batches = [b for _, b in items]
        seqs = [seq for seq, _ in items]
        attempt = 0
        self._group_in_flight = True
        while True:
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(self._live.persist, batches, session_id, seqs)
            except SQLAlchemyError as exc:
                attempt += 1
                if not self.persistence_failing:
                    self.counters["database_interruptions"] += 1
                    self.persisting_since = self._clock.now()
                    logger.error("stream.database_unavailable", error_type=type(exc).__name__)
                self.persistence_failing = True
                self.counters["persist_failures"] += 1
                self._error("database", f"write failed: {type(exc).__name__}")
                logger.warning(
                    "stream.persist_failed", error_type=type(exc).__name__, attempt=attempt
                )
                if (
                    stop.is_set()
                    and drain_deadline is not None
                    and time.monotonic() > drain_deadline
                ):
                    self._record_abandoned(batches, "database unavailable at shutdown")
                    self._group_in_flight = False
                    return False
                await asyncio.sleep(min(self._persist_retry_max, 0.5 * 2 ** min(attempt, 10)))
                continue
            except Exception as exc:  # a bug must not kill the stream; the loss is recorded
                logger.exception("stream.persist_crashed")
                self._error("internal", f"persisting failed: {type(exc).__name__}")
                self._record_abandoned(batches, f"internal error: {type(exc).__name__}")
                self._group_in_flight = False
                return True
            break
        committed = self._clock.now()
        if self.persistence_failing and self.persisting_since is not None:
            await asyncio.to_thread(
                self._persistence_recovered, self.persisting_since, committed, attempt
            )
        self.persistence_failing = False
        self.persisting_since = None
        self._group_in_flight = False
        self._persisted_through = max(b.raw_event.received_at for b in batches)
        if result.latest_bar_at is not None:
            self.last_bar_at = max(self.last_bar_at or result.latest_bar_at, result.latest_bar_at)
        self.last_persisted_at = committed
        for counter in (self.counters, self._session_counters):
            counter.update(result.counts)
        self._persist_seconds.add([time.monotonic() - started])
        self._commit_latency.add(
            [(committed - b.raw_event.received_at).total_seconds() for b in batches]
        )
        self._event_lag.add(result.event_lag_seconds)
        return True

    def _persistence_recovered(self, since: datetime, at: datetime, failed_writes: int) -> None:
        """Audit a database interruption once writes succeed again (nothing was lost while
        the bounded queue held the frames; losses are recorded separately as drops)."""
        try:
            with session_scope(self._sessions) as session:
                audit = AuditLogRepository(session)
                details = {
                    "provider": self._provider.provider_id,
                    "feed": self._provider.feed,
                    "failing_since": since.isoformat(),
                    "recovered_at": at.isoformat(),
                    "seconds": round((at - since).total_seconds(), 3),
                    "failed_writes": failed_writes,
                    "frames_dropped_total": self.counters["frames_dropped"],
                    "priority": "HIGH" if (at - since).total_seconds() > 60 else "NORMAL",
                }
                audit.append(
                    actor_type=ActorType.SUBSYSTEM,
                    actor_id="market_data_stream",
                    action="market_data.database_failure",
                    entity_type="market_data_stream",
                    entity_id=f"{self._provider.provider_id}:{self._provider.feed}",
                    reason="writes to PostgreSQL failed; frames were held in the bounded queue",
                    details=details,
                )
                audit.append(
                    actor_type=ActorType.SUBSYSTEM,
                    actor_id="market_data_stream",
                    action="market_data.persistence_recovered",
                    entity_type="market_data_stream",
                    entity_id=f"{self._provider.provider_id}:{self._provider.feed}",
                    details=details,
                )
        except SQLAlchemyError:
            logger.warning("stream.audit_failed", action="persistence_recovered")
        logger.info(
            "stream.database_recovered",
            seconds=round((at - since).total_seconds(), 3),
            failed_writes=failed_writes,
        )

    def _record_abandoned(self, batches: list[StreamBatch], reason: str) -> None:
        drops = self._drops
        for batch in batches:
            at = batch.raw_event.received_at
            drops.frames += 1
            drops.items += batch.item_count
            drops.first_at = min(drops.first_at or at, at)
            drops.last_at = max(drops.last_at or at, at)
        self._last_drop_at = self._clock.now()
        self._break_coverage()
        self.counters["frames_dropped"] += len(batches)
        logger.error("stream.frames_abandoned", frames=len(batches), reason=reason)

    async def _flush_drops(self) -> None:
        drops = self._drops
        if not drops.frames or drops.first_at is None or drops.last_at is None:
            return
        self._drops = _Drops()
        try:
            await asyncio.to_thread(self._record_drops, drops)
        except SQLAlchemyError:
            self._drops = drops  # retried on the next heartbeat
            logger.critical("stream.drop_record_failed", frames=drops.frames)

    def _record_drops(self, drops: _Drops) -> None:
        assert drops.first_at is not None  # noqa: S101
        assert drops.last_at is not None  # noqa: S101
        provider, feed = self._provider.provider_id, self._provider.feed
        with session_scope(self._sessions) as session:
            StreamRepository(session).open_gap(
                provider=provider,
                feed=feed,
                kind=StreamGapKind.DATA_DROPPED,
                gap_start=drops.first_at,
                gap_end=drops.last_at,
                reason=f"{drops.frames} received frame(s) ({drops.items} items) discarded: "
                "persistence could not keep up",
                symbols=sorted(self.desired.symbols),
                channels=sorted(c.value for c in self.desired.channels),
                detected_at=self._clock.now(),
                previous_session_id=self.session_id,
                last_message_before=drops.first_at,
                frames_dropped=drops.frames,
            )
        self._live.record_events(
            [
                self._live.event(
                    QualityIssue(
                        DataQualityEventType.DATA_DROPPED,
                        Severity.CRITICAL,
                        f"{drops.frames} received frame(s) with {drops.items} item(s) were "
                        "discarded because persistence could not keep up",
                        {"frames": drops.frames, "items": drops.items},
                    ),
                    interval=(drops.first_at, drops.last_at),
                )
            ]
        )

    # ------------------------------------------------------------ status, sessions, gaps
    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._flush_drops()
            with contextlib.suppress(SQLAlchemyError):
                await asyncio.to_thread(self._extend_coverage)
            await self._write_status()
            if self.session_id is not None:
                with contextlib.suppress(SQLAlchemyError):
                    await asyncio.to_thread(self._update_session)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_interval)

    # ------------------------------------------------------------ live coverage (2D)
    def _break_coverage(self) -> None:
        """Data was lost: freeze the current coverage segment."""
        self._segment_start = None
        self._segment_ids = {}
        self._drops_since_heartbeat = True

    def persisted_through(self) -> datetime | None:
        """Every frame received before the returned time is persisted."""
        if self._queue.empty() and not self._group_in_flight and not self.persistence_failing:
            return self._clock.now()
        return self._persisted_through

    def _extend_coverage(self) -> None:
        # Runs in a worker thread: read the loop-owned state once.
        session_id, subscribed_at = self.session_id, self._session_subscribed_at
        if self.state is not ConnectionState.SUBSCRIBED or session_id is None:
            return
        if self._drops_since_heartbeat:
            self._drops_since_heartbeat = False  # resume only after a drop-free interval
            return
        segment_start = self._segment_start
        if segment_start is None:
            now = self._clock.now()
            segment_start = max(subscribed_at or now, self._last_drop_at or subscribed_at or now)
            self._segment_start = segment_start
        through = self.persisted_through()
        if through is None or through <= segment_start:
            return
        targets = self._live.targets
        confirmed = self.confirmed
        minute = Timeframe.parse("1Min")
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            repo = CompletenessRepository(session)
            for provider_symbol, target in targets.items():
                wanted: list[tuple[DataType, tuple[datetime, datetime] | None, str | None]] = []
                if provider_symbol in confirmed.channel(StreamChannel.BARS):
                    wanted.append(
                        (
                            DataType.BAR,
                            live_bar_coverage(segment_start, through, minute),
                            "1Min",
                        )
                    )
                if provider_symbol in confirmed.channel(StreamChannel.TRADES):
                    wanted.append((DataType.TRADE, (segment_start, through), None))
                if provider_symbol in confirmed.channel(StreamChannel.QUOTES):
                    wanted.append((DataType.QUOTE, (segment_start, through), None))
                for data_type, interval, timeframe in wanted:
                    if interval is None:
                        continue
                    key = (target.asset_id, data_type)
                    existing = self._segment_ids.get(key)
                    if existing is not None:
                        repo.extend_coverage(existing, interval[1])
                        continue
                    series = Series(
                        target.asset_id,
                        self._provider.provider_id,
                        self._provider.feed,
                        data_type,
                        timeframe,
                        self._settings.historical_data_adjustment if timeframe else None,
                    )
                    self._segment_ids[key] = repo.add_coverage(
                        series,
                        interval,
                        CoverageEvidence.LIVE_STREAM,
                        at=now,
                        stream_session_id=session_id,
                    )

    async def _write_status(self, *, enabled: bool = True) -> None:
        try:
            await asyncio.to_thread(self._status_write, enabled)
        except SQLAlchemyError as exc:
            logger.warning("stream.status_write_failed", error_type=type(exc).__name__)

    def snapshot(self, *, enabled: bool = True) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "enabled": enabled,
            "connection_state": self.state.value,
            "auth_state": self.auth_state.value,
            "subscription_state": self.subscription_state.value,
            "current_session_id": self.session_id,
            "requested_subscription": self.desired.as_dict(),
            "confirmed_subscription": self.confirmed.as_dict(),
            "worker_started_at": self.started_at,
            "worker_stopped_at": self.stopped_at,
            "heartbeat_at": self._clock.now(),
            "last_authenticated_at": self.last_authenticated_at,
            "last_subscribed_at": self.last_subscribed_at,
            "last_message_at": self.last_message_at,
            "last_persisted_at": self.last_persisted_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "recent_errors": list(self.recent_errors),
            "reconnect_count": self.reconnect_count,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "counters": {name: self.counters.get(name, 0) for name in _ALL_COUNTERS},
            "metrics": {
                "persistence_failing": self.persistence_failing,
                "persistence_failing_since": (
                    self.persisting_since.isoformat() if self.persisting_since else None
                ),
                "persisted_through": (
                    self._persisted_through.isoformat() if self._persisted_through else None
                ),
                "last_completed_bar_at": self.last_bar_at.isoformat() if self.last_bar_at else None,
                "coverage_segment_start": (
                    self._segment_start.isoformat() if self._segment_start else None
                ),
                "last_drop_at": self._last_drop_at.isoformat() if self._last_drop_at else None,
                "commit_latency_seconds": self._commit_latency.summary(),
                "provider_event_lag_seconds": self._event_lag.summary(),
                "persist_group_seconds": self._persist_seconds.summary(),
            },
        }

    def _status_write(self, enabled: bool) -> None:
        values = self.snapshot(enabled=enabled)
        with session_scope(self._sessions) as session:
            StreamRepository(session).upsert_status(
                self._provider.provider_id, self._provider.feed, values
            )

    def _open_session(self, attempt: int, stream: Any) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            record = StreamRepository(session).create_session(
                StreamSessionRecord(
                    id=uuid.uuid4(),
                    provider=self._provider.provider_id,
                    feed=self._provider.feed,
                    worker_id=self.worker_id,
                    attempt=attempt,
                    state=ConnectionState.CONNECTING.value,
                    url=getattr(stream, "url", ""),
                    requested_subscription=self.desired.as_dict(),
                    started_at=self._clock.now(),
                )
            )
            return record.id

    def _session_values(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "confirmed_subscription": self.confirmed.as_dict(),
            "authenticated_at": self._session_authenticated_at,
            "subscribed_at": self._session_subscribed_at,
            "first_message_at": self._session_first_message_at,
            "last_message_at": self.last_message_at
            if self._session_first_message_at is not None
            else None,
            "last_persisted_at": self.last_persisted_at,
            "counters": dict(self._session_counters),
        }

    def _update_session(self) -> None:
        if self.session_id is None:
            return
        with session_scope(self._sessions) as session:
            values = self._session_values()
            # Never clear timestamps already recorded for this session.
            values = {k: v for k, v in values.items() if v is not None}
            StreamRepository(session).update_session(self.session_id, values)

    def _close_session(self, reason: str) -> None:
        if self.session_id is None:
            return
        try:
            with session_scope(self._sessions) as session:
                values = {k: v for k, v in self._session_values().items() if v is not None}
                values.update(
                    state=self.state.value,
                    ended_at=self._clock.now(),
                    end_reason=redact_text(reason)[:500],
                )
                StreamRepository(session).update_session(self.session_id, values)
        except SQLAlchemyError as exc:
            logger.warning("stream.session_close_failed", error_type=type(exc).__name__)

    def _record_previous_run(self) -> None:
        """Close sessions a crashed worker left open and record the uncovered interval."""
        provider, feed = self._provider.provider_id, self._provider.feed
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            repo = StreamRepository(session)
            repo.close_unfinished_sessions(provider, feed, at=now, reason=INTERRUPTED_REASON)
            previous = repo.latest_session(provider, feed)
            if previous is None or repo.gaps(provider, feed, open_only=True, limit=1):
                return
            last = previous.last_message_at
            start = previous.ended_at or now
            if last is not None:
                start = min(start, last)
            self._record_unpersisted(session, previous, now)
            repo.open_gap(
                provider=provider,
                feed=feed,
                kind=StreamGapKind.WORKER_NOT_RUNNING,
                gap_start=start,
                reason="no stream worker was running between the previous session and this one",
                symbols=sorted(self.desired.symbols),
                channels=sorted(c.value for c in self.desired.channels),
                detected_at=now,
                previous_session_id=previous.id,
                last_message_before=last,
            )

    def _record_unpersisted(
        self, session: Session, previous: StreamSessionRecord, now: datetime
    ) -> None:
        """Frames the previous worker received but never wrote were lost with its memory
        (it stopped without draining). Record them for recovery.

        Evidence, from the previous session's last heartbeat: ``frames_received`` above the
        frames written and dropped means frames were in flight. For 2D sessions the highest
        stored ``frame_seq`` gives an exact lower bound on the frames never written. The
        interval runs from the last written frame to one heartbeat after the last one
        recorded (the worker was gone by then).
        """
        if previous.subscribed_at is None:
            return
        counters = previous.counters or {}
        received = int(counters.get("frames_received", 0))
        in_flight = (
            received - int(counters.get("frames", 0)) - int(counters.get("frames_dropped", 0))
        )
        stored_seq, stored_at = session.execute(
            select(
                func.max(RawMarketDataEventRecord.frame_seq),
                func.max(RawMarketDataEventRecord.received_at),
            ).where(RawMarketDataEventRecord.stream_session_id == previous.id)
        ).one()
        lost = None if stored_seq is None else max(received - int(stored_seq), 0)
        if (lost if lost is not None else in_flight) <= 0:
            return
        start = stored_at or previous.subscribed_at
        last_seen = max(previous.last_message_at or start, start)
        end = last_seen + timedelta(seconds=self._heartbeat_interval)
        reason = (
            f"the previous worker stopped with received frames unwritten: {lost} frame(s) "
            "never stored (exact, from frame sequence numbers)"
            if lost is not None
            else f"the previous worker stopped with {in_flight} received frame(s) not yet "
            "written at its last heartbeat; those received after its last stored frame were "
            "lost (count unknown: the session predates frame sequence numbers)"
        )
        StreamRepository(session).open_gap(
            provider=previous.provider,
            feed=previous.feed,
            kind=StreamGapKind.DATA_NOT_PERSISTED,
            gap_start=start,
            gap_end=end,
            reason=reason,
            symbols=sorted(self.desired.symbols),
            channels=sorted(c.value for c in self.desired.channels),
            detected_at=now,
            previous_session_id=previous.id,
            last_message_before=start,
            frames_dropped=lost if lost is not None else 0,
        )
        session.add(
            self._live.event(
                QualityIssue(
                    DataQualityEventType.DATA_NOT_PERSISTED,
                    Severity.CRITICAL,
                    reason,
                    {
                        "session_id": str(previous.id),
                        "frames_lost": lost,
                        "frames_in_flight_at_last_heartbeat": in_flight,
                    },
                ),
                interval=(start, end),
            )
        )
        logger.critical("stream.data_not_persisted", frames_lost=lost, in_flight=in_flight)

    def _open_gap(self, kind: StreamGapKind, reason: str) -> None:
        provider, feed = self._provider.provider_id, self._provider.feed
        now = self._clock.now()
        try:
            with session_scope(self._sessions) as session:
                repo = StreamRepository(session)
                if repo.gaps(provider, feed, open_only=True, limit=1):
                    return  # an interruption is already open
                repo.open_gap(
                    provider=provider,
                    feed=feed,
                    kind=kind,
                    gap_start=now,
                    reason=redact_text(reason)[:500],
                    symbols=sorted(self.desired.symbols),
                    channels=sorted(c.value for c in self.desired.channels),
                    detected_at=now,
                    previous_session_id=self.session_id,
                    last_message_before=self.last_message_at,
                )
            self._live.record_events(
                [
                    self._live.event(
                        QualityIssue(
                            DataQualityEventType.PROVIDER_OUTAGE,
                            Severity.ERROR,
                            f"live stream interrupted: {redact_text(reason)[:300]}",
                            {"last_message_at": self.last_message_at},
                        ),
                        interval=(self.last_message_at or now, now),
                    )
                ]
            )
        except SQLAlchemyError as exc:
            logger.error("stream.gap_record_failed", error_type=type(exc).__name__)

    def _recovered(self) -> None:
        provider, feed = self._provider.provider_id, self._provider.feed
        now = self._clock.now()
        try:
            with session_scope(self._sessions) as session:
                closed = StreamRepository(session).close_open_gaps(
                    provider, feed, at=now, recovery_session_id=self.session_id
                )
            if closed:
                self._live.record_events(
                    [
                        self._live.event(
                            QualityIssue(
                                DataQualityEventType.RECOVERY,
                                Severity.INFO,
                                "live stream restored and subscriptions confirmed; the "
                                "interruption is recorded as a gap pending backfill (2D)",
                                {"gaps_closed": closed, "reconnect_count": self.reconnect_count},
                            ),
                            interval=(now, now),
                        )
                    ]
                )
        except SQLAlchemyError as exc:
            logger.error("stream.gap_close_failed", error_type=type(exc).__name__)

    # ------------------------------------------------------------ single-worker lock
    def _lock_key(self) -> str:
        return f"market_data_stream:{self._provider.provider_id}:{self._provider.feed}"

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
            logger.warning("stream.lock_release_failed")  # closing the connection releases it
        finally:
            connection.close()

    @staticmethod
    async def _finish_task(task: asyncio.Task[None], timeout: float) -> None:
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            logger.error("stream.task_shutdown_timeout", task=task.get_name())
        except Exception:
            logger.exception("stream.task_failed", task=task.get_name())


_ALL_COUNTERS = (
    "frames_received",
    "items_received",
    *COUNTER_NAMES,
    "frames_dropped",
    "items_dropped",
    "persist_failures",
    "reconnects",
    "database_interruptions",
)


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
        service=f"{settings.service_name}-stream",
        environment=settings.environment.value,
    )
    market_data = get_market_data_settings()
    clock = SystemClock()
    provider = create_provider(market_data, clock)
    engine = build_engine(settings)
    worker = StreamWorker(
        session_factory=build_session_factory(engine),
        provider=provider,
        settings=market_data,
        clock=clock,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)
    if duration is not None:
        loop.call_later(duration, stop.set)
    try:
        return await worker.run(stop)
    finally:
        await provider.aclose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.market_data_stream",
        description="Run the live market-data stream worker (market data only; no orders).",
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
    "StreamWorker",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())

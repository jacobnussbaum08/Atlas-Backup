"""Market-data recovery worker (Deliverable 2D): ``python -m app.workers.market_data_recovery``.

One process per provider/feed (PostgreSQL advisory lock), independent of the API and the
stream worker. Every ``MARKET_DATA_RECOVERY_INTERVAL`` seconds it runs one recovery cycle
(see ``app.data_engine.recovery``): detect gaps, schedule and run recovery jobs, verify
completeness. State is durable: gaps and jobs live in PostgreSQL, so a restart resumes
where the previous worker stopped (jobs it left RUNNING run again).

Disabled by default (``MARKET_DATA_RECOVERY_ENABLED=false``): it then records DISABLED
and exits 0, because recovery calls the provider's history.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import statistics
import sys
import time
import uuid
from collections import Counter, deque
from typing import Any

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
from app.data_engine.errors import MarketDataError
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.providers.registry import create_provider
from app.data_engine.recovery import RecoveryService
from app.database.repositories import RecoveryJobRepository, RecoveryStatusRepository
from app.database.sessions import build_engine, build_session_factory, session_scope

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_ALREADY_RUNNING = 3

MAX_RECENT_ERRORS = 20


class RecoveryWorker:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        settings: MarketDataSettings,
        clock: Clock | None = None,
        worker_id: str | None = None,
        interval: float | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock or SystemClock()
        self.worker_id = worker_id or (
            f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._interval = (
            interval if interval is not None else settings.market_data_recovery_interval
        )
        self.service = RecoveryService(
            session_factory, provider, settings, self._clock, worker_id=self.worker_id
        )
        self._lock_connection: Connection | None = None
        self.state = "STARTING"
        self.counters: Counter[str] = Counter()
        self.cycles = 0
        self.last_cycle_at: Any = None
        self.last_error: str | None = None
        self.last_error_at: Any = None
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_ERRORS)
        self._cycle_seconds: deque[float] = deque(maxlen=200)

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> int:
        if not self._settings.market_data_recovery_enabled and not once:
            self.state = "DISABLED"
            await self._write_status()
            logger.info("recovery.disabled", reason="MARKET_DATA_RECOVERY_ENABLED is false")
            return EXIT_OK
        if not self._acquire_lock():
            logger.error("recovery.already_running", provider=self._provider.provider_id)
            return EXIT_ALREADY_RUNNING
        try:
            try:
                self.service.tracked()  # validates symbols and timeframes up front
            except (ValueError, MarketDataError) as exc:
                self.state = "FAILED"
                self._error("configuration", str(exc))
                await self._write_status()
                return EXIT_CONFIGURATION
            await asyncio.to_thread(self.service.recover_interrupted)
            self.state = "RUNNING"
            while not stop.is_set():
                await self._cycle()
                if once:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._interval)
            self.state = "STOPPED"
            await self._write_status()
            return EXIT_OK
        finally:
            self._release_lock()

    async def _cycle(self) -> None:
        started = time.monotonic()
        self.state = "RUNNING"
        await self._write_status()
        try:
            report = await self.service.run_cycle()
            self.counters.update(report.counts)
            self.state = "IDLE"
        except SQLAlchemyError as exc:
            self.state = "DEGRADED"
            self._error("database", f"cycle failed: {type(exc).__name__}")
            logger.warning("recovery.cycle_failed", error_type=type(exc).__name__)
        except Exception as exc:  # the worker survives; the failure is reported
            self.state = "DEGRADED"
            self._error("internal", f"cycle failed: {type(exc).__name__}")
            logger.exception("recovery.cycle_crashed")
        self.cycles += 1
        self.last_cycle_at = self._clock.now()
        self._cycle_seconds.append(time.monotonic() - started)
        await self._write_status()
        logger.info("recovery.cycle", state=self.state, counters=dict(self.counters))

    def _error(self, kind: str, message: str) -> None:
        message = redact_text(message)[:500]
        self.last_error, self.last_error_at = message, self._clock.now()
        self.recent_errors.append(
            {"at": self.last_error_at.isoformat(), "kind": kind, "message": message}
        )

    async def _write_status(self) -> None:
        try:
            await asyncio.to_thread(self._status_write)
        except SQLAlchemyError as exc:
            logger.warning("recovery.status_write_failed", error_type=type(exc).__name__)

    def _status_write(self) -> None:
        provider, feed = self._provider.provider_id, self._provider.feed
        seconds = sorted(self._cycle_seconds)
        with session_scope(self._sessions) as session:
            last_backfill = RecoveryJobRepository(session).last_completed_at(provider, feed)
            RecoveryStatusRepository(session).upsert(
                provider,
                feed,
                {
                    "worker_id": self.worker_id,
                    "enabled": self._settings.market_data_recovery_enabled,
                    "state": self.state,
                    "heartbeat_at": self._clock.now(),
                    "last_cycle_at": self.last_cycle_at,
                    "last_successful_backfill_at": last_backfill,
                    "last_error": self.last_error,
                    "last_error_at": self.last_error_at,
                    "recent_errors": list(self.recent_errors),
                    "counters": dict(self.counters),
                    "metrics": {
                        "cycles": self.cycles,
                        "interval_seconds": self._interval,
                        "cycle_seconds": None
                        if not seconds
                        else {
                            "count": len(seconds),
                            "mean": round(statistics.fmean(seconds), 3),
                            "max": round(seconds[-1], 3),
                        },
                    },
                },
            )

    # ------------------------------------------------------------ single-worker lock
    def _lock_key(self) -> str:
        return f"market_data_recovery:{self._provider.provider_id}:{self._provider.feed}"

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
            logger.warning("recovery.lock_release_failed")  # closing the connection releases it
        finally:
            connection.close()


async def _main_async(once: bool, duration: float | None) -> int:
    from app.workers.market_data_stream import _install_signal_handlers

    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=f"{settings.service_name}-recovery",
        environment=settings.environment.value,
    )
    market_data = get_market_data_settings()
    clock = SystemClock()
    provider = create_provider(market_data, clock)
    engine = build_engine(settings)
    worker = RecoveryWorker(
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
        return await worker.run(stop, once=once)
    finally:
        await provider.aclose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.market_data_recovery",
        description="Run the market-data recovery worker (market data only; no orders).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit (an explicit operator action: runs even when disabled)",
    )
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args.once, args.duration))
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError, MarketDataError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_CONFIGURATION


__all__ = ["EXIT_ALREADY_RUNNING", "EXIT_CONFIGURATION", "EXIT_OK", "RecoveryWorker", "main"]

if __name__ == "__main__":
    raise SystemExit(main())

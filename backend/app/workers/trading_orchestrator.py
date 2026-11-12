"""Trading orchestrator worker (Phase 10A): ``python -m app.workers.trading_orchestrator``.

**Paper trading only.** Each tick (see :mod:`app.orchestration.orchestrator`) expires due
reservations, runs a strategy-evaluation cycle and, when new proposals arrived, a LIVE AI
decision run; when the operator control is ENABLED it takes prioritized decisions in as
trade intents and delivers them (risk reservation, then a simulated paper order through the
7A execution engine); it runs the scheduled jobs (risk monitor, reconciliations, worker
health, market intelligence and decision outcomes after the close) and records a heartbeat.

Off unless ``ORCHESTRATOR_WORKER_ENABLED=true``. Running the worker does **not** enable
trading: new entries also need the operator control ENABLED (``python -m
app.orchestration.cli enable --actor operator:<name> --reason ...``), which is DISABLED until
an operator changes it. The paper execution worker and the position monitor must run too:
they deliver orders to the simulated paper broker, simulate fills and manage exits.

SIGTERM/SIGINT finish the step in hand and record STOPPED. Every step is a PostgreSQL row and
idempotent: a crash loses nothing and a restart resumes (intents stay RECEIVED with their
outbox event PENDING; a job whose lease expired is reclaimed). If the database is
unreachable the tick fails, the worker keeps running and retries on the next tick.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from pydantic import ValidationError

from app.accounting.config import get_accounting_settings
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging, get_logger
from app.core.security.redaction import redact_text
from app.data_engine.errors import MarketDataError
from app.database.sessions import build_engine, build_session_factory
from app.orchestration.config import OrchestratorSettings, get_orchestrator_settings
from app.orchestration.enums import OrchestratorState
from app.orchestration.orchestrator import Orchestrator
from app.orchestration.runtime import build_orchestrator
from app.strategies.engine import worker_identity
from app.workers.strategy_evaluation import _install_signal_handlers

logger = get_logger(__name__)

EXIT_OK, EXIT_FAILURE, EXIT_CONFIGURATION = 0, 1, 2
HEARTBEAT_SECONDS = 30.0


def heartbeat_interval(settings: OrchestratorSettings) -> float:
    return min(settings.tick_seconds, HEARTBEAT_SECONDS)


def _safe_status(
    orchestrator: Orchestrator,
    state: OrchestratorState,
    *,
    error: str | None = None,
    started: bool = False,
    stopped: bool = False,
) -> None:
    """Record a status; when the database is unreachable, log and go on (the API then shows
    the heartbeat going stale, which is the truth)."""
    try:
        orchestrator.write_status(state, error=error, started=started, stopped=stopped)
    except Exception as exc:
        logger.warning("orchestration.status_unrecorded", error_type=type(exc).__name__)


async def _heartbeat(orchestrator: Orchestrator, interval: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(orchestrator.beat)
        except Exception as exc:  # the tick reports the outage; keep beating
            logger.warning("orchestration.heartbeat_failed", error_type=type(exc).__name__)


async def run(
    orchestrator: Orchestrator,
    settings: OrchestratorSettings,
    stop: asyncio.Event,
    *,
    max_ticks: int | None = None,
) -> int:
    """Tick until ``stop`` is set (or ``max_ticks`` ticks ran)."""
    await asyncio.to_thread(_safe_status, orchestrator, OrchestratorState.STARTING, started=True)
    logger.info(
        "orchestration.started",
        simulated=True,
        tick_seconds=settings.tick_seconds,
        worker_id=orchestrator.worker_id,
    )
    beats = asyncio.create_task(_heartbeat(orchestrator, heartbeat_interval(settings), stop))
    ticks = 0
    try:
        while not stop.is_set():
            try:
                await orchestrator.tick()
            except Exception as exc:  # e.g. the database is down: retry on the next tick
                orchestrator.counters["tick_failures"] = (
                    orchestrator.counters.get("tick_failures", 0) + 1
                )
                logger.warning("orchestration.tick_failed", error_type=type(exc).__name__)
                await asyncio.to_thread(
                    _safe_status,
                    orchestrator,
                    OrchestratorState.DEGRADED,
                    error=redact_text(f"tick failed: {type(exc).__name__}: {exc}"),
                )
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.tick_seconds)
    finally:
        stop.set()
        beats.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beats
        await asyncio.to_thread(_safe_status, orchestrator, OrchestratorState.STOPPED, stopped=True)
        logger.info("orchestration.stopped", simulated=True, ticks=ticks)
    return EXIT_OK


async def _main_async(duration: float | None, once: bool) -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=f"{settings.service_name}-trading-orchestrator",
        environment=settings.environment.value,
    )
    accounting = get_accounting_settings()
    orchestration = get_orchestrator_settings()
    if not orchestration.worker_enabled:
        logger.info("orchestration.worker_disabled", setting="ORCHESTRATOR_WORKER_ENABLED")
        return EXIT_OK
    engine = build_engine(settings)
    try:
        runtime = await asyncio.to_thread(
            build_orchestrator,
            build_session_factory(engine),
            SystemClock(),
            orchestration,
            worker_id=f"orchestrator:{worker_identity()}",
            accounting=accounting,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop)
        if duration is not None:
            loop.call_later(duration, stop.set)
        return await run(runtime.orchestrator, orchestration, stop, max_ticks=1 if once else None)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.trading_orchestrator",
        description="Autonomous paper-trading orchestrator (simulated paper orders only).",
    )
    parser.add_argument(
        "--duration", type=float, help="stop after this many seconds (default: until signalled)"
    )
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args.duration, args.once))
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError, MarketDataError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_CONFIGURATION


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

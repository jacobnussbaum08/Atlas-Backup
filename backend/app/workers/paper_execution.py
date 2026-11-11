"""Paper execution worker (Phases 7A-7B): ``python -m app.workers.paper_execution``.

**Simulated execution only.** It drives the isolated paper broker; it has no brokerage
connection, credential or live mode.

Lifecycle::

    start: paper broker RUNNING + recovery (apply broker events whose delivery was lost,
           resolve dead submissions/cancellations, expire due orders)
    loop:  heartbeat -> deliver due outbox events -> simulate working orders against the
           market observations that became available (Phase 7B) -> expire due orders
           -> sleep
    SIGTERM/SIGINT: finish the event in hand, mark the paper broker STOPPED

Everything it handles is a PostgreSQL row, so a crash loses nothing: an event whose
handler did not commit stays PENDING and is retried, and the next start runs recovery.
Several workers may run at once: each outbox event is claimed with SKIP LOCKED and every
handler is idempotent.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from types import FrameType

from pydantic import ValidationError

from app.accounting.config import get_accounting_settings
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.logging import configure_logging, get_logger
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.execution.runtime import ExecutionContext, build_execution_context

logger = get_logger(__name__)


def run(ctx: ExecutionContext, stop: threading.Event, *, interval: float, batch: int) -> int:
    report = ctx.service.recover()
    logger.info(
        "paper_execution.started",
        simulated=True,
        live_orders=report.live_orders_checked,
        broker_events_applied=report.broker_events_applied,
        recovery_errors=len(report.errors),
    )
    dispatcher = ctx.service.dispatcher()
    try:
        while not stop.is_set():
            with session_scope(ctx.session_factory) as session:
                ctx.broker.heartbeat(session, ctx.clock.now())
            summary = dispatcher.dispatch(limit=batch)
            simulated = ctx.simulator.run_once()
            if simulated.fills or simulated.triggers:
                summary = dispatcher.dispatch(limit=batch)
            expired = ctx.service.expire_due()
            if (
                summary.delivered
                or summary.failed
                or summary.dead
                or expired.expired
                or simulated.fills
                or simulated.triggers
                or simulated.errors
            ):
                logger.info(
                    "paper_execution.tick",
                    delivered=summary.delivered,
                    failed=summary.failed,
                    dead=summary.dead,
                    expired=len(expired.expired),
                    simulated_fills=simulated.fills,
                    triggers=simulated.triggers,
                    simulation_errors=len(simulated.errors),
                )
            if summary.delivered < batch:
                stop.wait(interval)
    finally:
        with session_scope(ctx.session_factory) as session:
            ctx.broker.shutdown(session, ctx.clock.now())
        logger.info("paper_execution.stopped", simulated=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.paper_execution")
    parser.add_argument("--interval", type=float, default=1.0, help="idle sleep in seconds")
    parser.add_argument("--batch", type=int, default=100, help="outbox events per tick")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        get_accounting_settings()
    except ValidationError as exc:
        print(f"Invalid Atlas configuration:\n{describe_validation_error(exc)}", file=sys.stderr)
        return 2
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid Atlas configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, json_output=settings.log_json)
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("paper_execution.signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    engine = build_engine(settings)
    try:
        ctx = build_execution_context(build_session_factory(engine), SystemClock())
        return run(ctx, stop, interval=args.interval, batch=args.batch)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

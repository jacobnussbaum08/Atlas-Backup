"""Position-monitoring worker (Phase 7B): ``python -m app.workers.position_monitor``.

**Simulated positions only.** Each pass (see :mod:`app.execution.monitor`) marks the open
paper positions with the newest fresh stored price, evaluates their protective levels
against the observations that arrived since the previous pass, submits exits (simulated
paper orders) when a stop, trailing stop, invalidation level, expiry or operator close
request calls for one, keeps the resting take-profit orders in place, accrues short borrow
costs and takes periodic 6A valuation checkpoints.

Polling is modest by design: the default interval is 5 seconds (protective levels are
checked against *every* stored trade and bar since the last pass, so nothing is missed
between passes; a shorter interval only shortens the execution delay of an exit). Outside
the equity session nothing triggers, and a pass with nothing open costs one query.

SIGTERM/SIGINT finish the position in hand. Everything is a PostgreSQL row: a crash loses
nothing, and exits are submitted with idempotency keys derived from the position, so a
restart cannot submit an exit twice. Several workers may run at once (each position is
handled under its account lock).
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
from app.database.sessions import build_engine, build_session_factory
from app.execution.runtime import ExecutionContext, build_execution_context

logger = get_logger(__name__)


def run(ctx: ExecutionContext, stop: threading.Event, *, interval: float) -> int:
    logger.info("position_monitor.started", simulated=True, interval=interval)
    while not stop.is_set():
        summary = ctx.monitor.run_once()
        if (
            summary.triggers
            or summary.exits_submitted
            or summary.take_profits_placed
            or summary.cancels_requested
            or summary.errors
        ):
            logger.info(
                "position_monitor.tick",
                positions=summary.positions,
                triggers=summary.triggers,
                exits=summary.exits_submitted,
                take_profits=summary.take_profits_placed,
                cancels=summary.cancels_requested,
                borrow_accruals=summary.borrow_accruals,
                valuations=summary.valuations,
                stale=summary.stale,
                errors=len(summary.errors),
            )
        stop.wait(interval)
    logger.info("position_monitor.stopped", simulated=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.position_monitor")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between passes")
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
        logger.info("position_monitor.signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    engine = build_engine(settings)
    try:
        ctx = build_execution_context(build_session_factory(engine), SystemClock())
        return run(ctx, stop, interval=max(args.interval, 0.5))
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

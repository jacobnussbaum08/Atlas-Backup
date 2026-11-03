"""Strategy evaluation worker (Phase 4A): ``python -m app.workers.strategy_evaluation``.

Every ``STRATEGY_WORKER_INTERVAL_SECONDS``::

    record expiry of proposals whose expiry passed
    -> detect validated updates (completed daily/weekly periods with a VALID stored bar,
       news events about qualified-universe members since the previous check)
    -> plan: implemented, enabled strategies whose schedule, timeframe and asset class match
    -> evaluate concurrently (bounded, with a timeout each); duplicates are skipped by
       idempotency key, failures are recorded and retried up to STRATEGY_MAX_ATTEMPTS

On start it registers the catalog (feature definitions, strategies, versions). Several
workers may run: evaluations are claimed under advisory locks and idempotency keys. The
worker produces proposals only; nothing here sizes, approves or submits a trade.

Phase 4A ships no strategy with evaluation logic, so until Phase 4B the worker records
expiry and finds nothing to evaluate. Disabled by default (``STRATEGY_WORKER_ENABLED``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.core.clocks import Clock, SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging, get_logger
from app.data_engine.errors import MarketDataError
from app.data_engine.runtime import build_market_data_context
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.strategies import catalog, triggers
from app.strategies.runtime import StrategyContext, build_strategy_context

logger = get_logger(__name__)

EXIT_OK, EXIT_CONFIGURATION = 0, 2


class StrategyEvaluationWorker:
    def __init__(self, context: StrategyContext, clock: Clock) -> None:
        self.context = context
        self.clock = clock
        lookback = timedelta(minutes=context.settings.news_trigger_lookback_minutes)
        self._news_since: datetime = clock.now() - lookback

    def sync(self) -> catalog.SyncReport:
        with session_scope(self.context.session_factory) as session:
            report = catalog.sync_catalog(session, self.context.registry)
        for problem in report.problems:
            logger.warning("strategy.catalog_problem", problem=problem)
        return report

    async def cycle(self) -> dict[str, Any]:
        """One pass: expiry, detection, planning, evaluation."""
        engine = self.context.engine
        expired = await asyncio.to_thread(engine.expire_due)
        now = self.clock.now()
        with session_scope(self.context.session_factory) as session:
            assets = triggers.scope(session, now)
            updates = triggers.bar_close_updates(session, now, engine.series, assets)
            updates += triggers.news_updates(session, self._news_since, now, assets)
        self._news_since = now
        requests = await asyncio.to_thread(engine.plan, updates)
        results = await engine.evaluate_many(requests)
        outcomes: dict[str, int] = {}
        for result in results:
            evaluated = result.outcome == "EVALUATED" and result.status is not None
            label = result.status.value if evaluated and result.status else result.outcome
            outcomes[label] = outcomes.get(label, 0) + 1
        summary = {
            "expired": expired,
            "assets": len(assets),
            "updates": len(updates),
            "requests": len(requests),
            "outcomes": outcomes,
        }
        logger.info("strategy.cycle", **summary)
        return summary

    async def run(self, stop: asyncio.Event) -> int:
        if not self.context.settings.worker_enabled:
            logger.info("strategy.worker_disabled")
            return EXIT_OK
        await asyncio.to_thread(self.sync)
        interval = self.context.settings.worker_interval_seconds
        while not stop.is_set():
            try:
                await self.cycle()
            except Exception:  # keep running; the next cycle retries (idempotent)
                logger.exception("strategy.cycle_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
        logger.info("strategy.worker_stopped")
        return EXIT_OK


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
        service=f"{settings.service_name}-strategy-evaluation",
        environment=settings.environment.value,
    )
    clock = SystemClock()
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    market = build_market_data_context(sessions, clock)
    context = build_strategy_context(
        sessions, clock, provider=market.provider.provider_id, feed=market.provider.feed
    )
    worker = StrategyEvaluationWorker(context, clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)
    if duration is not None:
        loop.call_later(duration, stop.set)
    try:
        return await worker.run(stop)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.strategy_evaluation",
        description="Evaluate enabled strategies on completed periods and news (proposals "
        "only; no orders).",
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
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_CONFIGURATION


if __name__ == "__main__":
    raise SystemExit(main())

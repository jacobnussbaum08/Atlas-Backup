"""AI decision engine worker (Phase 5B): ``python -m app.workers.ai_engine``.

Three independent loops, each off unless enabled:

* ``AI_DECISIONS_ENABLED``: a LIVE coordinator run every ``AI_DECISIONS_INTERVAL_SECONDS``
  (it requests point-in-time predictions of APPROVED/SHADOW models first; repeated runs at
  the same instant are idempotent).
* ``AI_NEWS_ANALYSIS_ENABLED``: an LLM news-analysis cycle every
  ``AI_NEWS_ANALYSIS_INTERVAL_SECONDS`` (cached, deduplicated, budgeted).
* ``AI_CONFLICT_EXPLANATIONS_ENABLED``: LLM explanations of recorded conflicts.

The loops are separate tasks: a slow or unavailable LLM provider never delays a decision
run, and decisions only read LLM analyses already stored and available. Nothing here
sizes, approves or submits a trade.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from pydantic import ValidationError

from app.ai_engine.enums import DecisionMode
from app.ai_engine.llm.providers import build_provider
from app.ai_engine.llm.service import LLMService
from app.ai_engine.llm.tasks import ConflictExplainer, NewsAnalyzer
from app.ai_engine.runtime import AIContext, build_ai_context
from app.core.clocks import SystemClock
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
from app.database.sessions import build_engine, build_session_factory
from app.ml_engine.runtime import build_ml_context
from app.strategies.engine import worker_identity
from app.workers.strategy_evaluation import _install_signal_handlers

logger = get_logger(__name__)

EXIT_OK, EXIT_CONFIGURATION = 0, 2


async def _loop(
    name: str, interval: float, body: Callable[[], Awaitable[object]], stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await body()
        except Exception:  # keep running; the next cycle retries
            logger.exception("ai.worker_cycle_failed", loop=name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)


class AIWorker:
    def __init__(self, context: AIContext, service: LLMService) -> None:
        self.context = context
        self.service = service

    async def decide(self) -> object:
        return await asyncio.to_thread(
            self.context.coordinator.run,
            self.context.policy,
            mode=DecisionMode.LIVE,
            trigger="worker",
            actor="ai-worker",
        )

    async def analyze(self) -> object:
        await asyncio.to_thread(self.service.sweep_interrupted)
        settings = self.context.settings
        return await NewsAnalyzer(
            self.context.session_factory, self.service, self.context.clock
        ).run_cycle(
            lookback=timedelta(hours=settings.news_analysis_lookback_hours),
            limit=settings.news_analysis_max_articles_per_cycle,
        )

    async def explain(self) -> object:
        explainer = ConflictExplainer(
            self.context.session_factory, self.service, self.context.clock
        )
        for run_id in await asyncio.to_thread(explainer.pending_runs, 3):
            await explainer.explain(run_id)
        return None

    async def run(self, stop: asyncio.Event) -> int:
        s = self.context.settings
        loops = []
        if s.decisions_enabled:
            loops.append(_loop("decisions", s.decisions_interval_seconds, self.decide, stop))
        if s.news_analysis_enabled and self.service.available:
            loops.append(_loop("news", s.news_analysis_interval_seconds, self.analyze, stop))
        if s.conflict_explanations_enabled and self.service.available:
            loops.append(_loop("explain", s.news_analysis_interval_seconds, self.explain, stop))
        if (
            s.news_analysis_enabled or s.conflict_explanations_enabled
        ) and not self.service.available:
            logger.warning("ai.llm_unavailable", reason="AI_LLM_PROVIDER is none")
        if not loops:
            logger.info("ai.worker_disabled")
            return EXIT_OK
        await asyncio.gather(*loops)
        logger.info("ai.worker_stopped")
        return EXIT_OK


async def _main_async(duration: float | None) -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=f"{settings.service_name}-ai-engine",
        environment=settings.environment.value,
    )
    clock = SystemClock()
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    market = build_market_data_context(sessions, clock)
    ml = build_ml_context(
        sessions, clock, provider=market.provider.provider_id, feed=market.provider.feed
    )
    context = build_ai_context(sessions, clock, inference=ml.inference, worker_id=worker_identity())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)
    if duration is not None:
        loop.call_later(duration, stop.set)
    try:
        async with httpx.AsyncClient() as client:
            service = LLMService(
                sessions,
                context.settings,
                clock,
                build_provider(context.settings, client),
                worker_id=context.coordinator.worker_id,
            )
            return await AIWorker(context, service).run(stop)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.ai_engine",
        description="Decision coordinator and asynchronous LLM analysis (no orders).",
    )
    parser.add_argument(
        "--duration", type=float, help="stop after this many seconds (default: until signalled)"
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

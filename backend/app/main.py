"""FastAPI application factory.

Startup order: validate settings -> configure logging -> build dependencies.
Invalid configuration raises before the server accepts a single request.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.api import health
from app.api import v1 as api_v1
from app.api.middleware import request_context_middleware
from app.core.clocks import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.redis_client import build_redis
from app.core.security import TRADING_MODE
from app.data_engine.historical import INTERRUPTED_REASON
from app.data_engine.historical import STALE_AFTER as INGESTION_STALE_AFTER
from app.data_engine.runtime import MarketDataContext, build_market_data_context

if TYPE_CHECKING:
    from app.accounting.runtime import AccountingContext
    from app.ai_engine.runtime import AIContext
    from app.asset_discovery.runtime import AssetDiscoveryContext
    from app.market_intelligence.runtime import IntelligenceContext
    from app.ml_engine.runtime import MLContext
    from app.news.runtime import NewsContext
    from app.risk.runtime import RiskContext
    from app.strategies.runtime import StrategyContext
from app.database.repositories import IngestionJobRepository
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.monitoring.health import (
    HealthCheck,
    check_database,
    check_migrations,
    check_redis,
    expected_migration_heads,
)

logger = get_logger(__name__)

INTERRUPTED_SWEEP_SECONDS = 60.0


def _package_version() -> str:
    try:
        return version("atlas-ai-trader")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _recover_interrupted_jobs(session_factory: sessionmaker[Session], clock: Clock) -> None:
    """Fail ingestion jobs whose heartbeat went stale (their process died).

    Other processes (recovery and discovery workers) run ingestion jobs too: a job with a
    fresh heartbeat is someone's live work and is never touched.
    """
    now = clock.now()
    try:
        with session_scope(session_factory) as session:
            count = IngestionJobRepository(session).mark_interrupted(
                reason=INTERRUPTED_REASON, at=now, stale_before=now - INGESTION_STALE_AFTER
            )
    except Exception as exc:  # the database may be down; /ready reports that
        logger.warning("historical.recovery_skipped", error_type=type(exc).__name__)
        return
    if count:
        logger.warning("historical.jobs_interrupted", count=count)


async def _sweep_interrupted_jobs(session_factory: sessionmaker[Session], clock: Clock) -> None:
    """Repeat the sweep: jobs orphaned just before a restart only go stale afterwards."""
    while True:
        await asyncio.sleep(INTERRUPTED_SWEEP_SECONDS)
        await asyncio.to_thread(_recover_interrupted_jobs, session_factory, clock)


def _build_news_context(session_factory: sessionmaker[Session], clock: Clock) -> NewsContext:
    from app.news.runtime import build_news_context

    return build_news_context(session_factory, clock)


def _build_strategy_context(
    session_factory: sessionmaker[Session], clock: Clock, provider: str, feed: str
) -> StrategyContext:
    from app.strategies.runtime import build_strategy_context

    return build_strategy_context(session_factory, clock, provider=provider, feed=feed)


def _build_ml_context(
    session_factory: sessionmaker[Session], clock: Clock, provider: str, feed: str
) -> MLContext:
    from app.ml_engine.runtime import build_ml_context

    return build_ml_context(session_factory, clock, provider=provider, feed=feed)


def _build_ai_context(session_factory: sessionmaker[Session], clock: Clock) -> AIContext:
    from app.ai_engine.runtime import build_ai_context

    # The API only reads: no inference engine, no LLM provider.
    return build_ai_context(session_factory, clock)


def _build_accounting_context(
    session_factory: sessionmaker[Session], clock: Clock
) -> AccountingContext:
    from app.accounting.runtime import build_accounting_context

    # The API only reads: postings, checkpoints and reconciliation are CLI jobs.
    return build_accounting_context(session_factory, clock)


def _build_risk_context(session_factory: sessionmaker[Session], clock: Clock) -> RiskContext:
    from app.risk.runtime import build_risk_context

    # The API reads, plus the fail-safe kill-switch engage; limits, resets, releases and
    # assessments are operator CLI actions.
    return build_risk_context(session_factory, clock)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    health_checks: Mapping[str, HealthCheck] | None = None,
    market_data: MarketDataContext | None = None,
    asset_discovery: AssetDiscoveryContext | None = None,
    market_intelligence: IntelligenceContext | None = None,
    news: NewsContext | None = None,
    strategies: StrategyContext | None = None,
    ml: MLContext | None = None,
    ai: AIContext | None = None,
    accounting: AccountingContext | None = None,
    risk: RiskContext | None = None,
) -> FastAPI:
    """Build the application.

    ``health_checks`` replaces the real dependency checks; when given, no
    database engine or Redis client is created (used by unit tests).
    ``market_data``, ``asset_discovery``, ``market_intelligence``, ``news``,
    ``strategies``, ``ml``, ``ai``, ``accounting`` and ``risk`` inject those contexts
    (tests); otherwise they are built lazily from the environment on first use.
    """
    settings = settings or get_settings()
    clock = clock or SystemClock()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=settings.service_name,
        environment=settings.environment.value,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = redis_client = None
        sweeper: asyncio.Task[None] | None = None
        if health_checks is None:
            engine = build_engine(settings)
            redis_client = build_redis(settings)
            heads = expected_migration_heads()
            app.state.engine = engine
            app.state.session_factory = build_session_factory(engine)
            app.state.redis = redis_client
            app.state.health_checks = {
                "database": lambda: check_database(engine),
                "migrations": lambda: check_migrations(engine, heads),
                "redis": lambda: check_redis(redis_client),
            }
            session_factory = app.state.session_factory
            app.state.market_data_factory = lambda: build_market_data_context(
                session_factory, clock
            )
            app.state.news_factory = lambda: _build_news_context(session_factory, clock)
            app.state.strategies_factory = lambda provider, feed: _build_strategy_context(
                session_factory, clock, provider, feed
            )
            app.state.ml_factory = lambda provider, feed: _build_ml_context(
                session_factory, clock, provider, feed
            )
            app.state.ai_factory = lambda: _build_ai_context(session_factory, clock)
            app.state.accounting_factory = lambda: _build_accounting_context(session_factory, clock)
            app.state.risk_factory = lambda: _build_risk_context(session_factory, clock)
            _recover_interrupted_jobs(session_factory, clock)
            sweeper = asyncio.create_task(_sweep_interrupted_jobs(session_factory, clock))
        else:
            app.state.health_checks = dict(health_checks)

        logger.info(
            "app.startup",
            version=app.state.version,
            trading_mode=TRADING_MODE.value,
            database=settings.database_url.render_as_string(hide_password=True),
            redis=str(settings.redis_url),
        )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            live_hub = getattr(app.state, "live_hub", None)
            if live_hub is not None:
                await live_hub.close()
            if redis_client is not None:
                redis_client.close()
            if engine is not None:
                engine.dispose()
            logger.info("app.shutdown")

    app = FastAPI(
        title="Atlas AI Trader",
        version=_package_version(),
        summary="Paper-trading research platform. Simulated execution only.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.market_data = market_data
    app.state.asset_discovery = asset_discovery
    app.state.market_intelligence = market_intelligence
    app.state.news = news
    app.state.strategies = strategies
    app.state.strategies_sync = None
    app.state.ml = ml
    app.state.ai = ai
    app.state.accounting = accounting
    app.state.risk = risk
    app.state.version = app.version
    app.state.started_at = clock.now()

    app.middleware("http")(request_context_middleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(origin).rstrip("/") for origin in settings.cors_origins],
            allow_methods=["GET"],
            allow_headers=["X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_v1.router)
    return app

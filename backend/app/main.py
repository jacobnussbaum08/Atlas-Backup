"""FastAPI application factory.

Startup order: validate settings -> configure logging -> build dependencies.
Invalid configuration raises before the server accepts a single request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

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
from app.data_engine.runtime import MarketDataContext, build_market_data_context
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


def _package_version() -> str:
    try:
        return version("atlas-ai-trader")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _recover_interrupted_jobs(session_factory: sessionmaker[Session], clock: Clock) -> None:
    """Jobs run in-process, so any job still PENDING/RUNNING at startup was orphaned."""
    try:
        with session_scope(session_factory) as session:
            count = IngestionJobRepository(session).mark_interrupted(
                reason=INTERRUPTED_REASON, at=clock.now()
            )
    except Exception as exc:  # the database may be down; /ready reports that
        logger.warning("historical.recovery_skipped", error_type=type(exc).__name__)
        return
    if count:
        logger.warning("historical.jobs_interrupted", count=count)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    health_checks: Mapping[str, HealthCheck] | None = None,
    market_data: MarketDataContext | None = None,
) -> FastAPI:
    """Build the application.

    ``health_checks`` replaces the real dependency checks; when given, no
    database engine or Redis client is created (used by unit tests).
    ``market_data`` injects the market-data context (tests); otherwise it is built
    lazily from the environment on first use.
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
            _recover_interrupted_jobs(session_factory, clock)
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

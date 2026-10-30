"""Wiring of the market-data components for the API and the CLI."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.config import MarketDataSettings, get_market_data_settings
from app.core.logging import get_logger
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.providers.registry import create_provider

logger = get_logger(__name__)


@dataclass(frozen=True)
class MarketDataContext:
    settings: MarketDataSettings
    provider: MarketDataProvider
    service: HistoricalIngestionService
    session_factory: sessionmaker[Session]


def build_market_data_context(
    session_factory: sessionmaker[Session], clock: Clock
) -> MarketDataContext:
    settings = get_market_data_settings()
    provider = create_provider(settings, clock)
    service = HistoricalIngestionService(session_factory, provider, settings, clock)
    return MarketDataContext(settings, provider, service, session_factory)


def run_job_blocking(service: HistoricalIngestionService, job_id: uuid.UUID) -> None:
    """Run a job to completion on its own event loop (used from a worker thread)."""
    try:
        asyncio.run(service.run_job(job_id))
    except Exception:
        # run_job has already recorded the failure on the job; keep the worker alive.
        logger.exception("historical.background_job_failed", job_id=str(job_id))

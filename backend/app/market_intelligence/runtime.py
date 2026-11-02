"""Wiring of the market intelligence components for the API and the CLI."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.runtime import MarketDataContext
from app.market_intelligence.config import (
    MarketIntelligenceSettings,
    get_market_intelligence_settings,
)
from app.market_intelligence.service import MarketIntelligenceService

logger = get_logger(__name__)


@dataclass(frozen=True)
class IntelligenceContext:
    settings: MarketIntelligenceSettings
    provider: MarketDataProvider
    service: MarketIntelligenceService
    session_factory: sessionmaker[Session]
    clock: Clock


def build_intelligence_context(
    market: MarketDataContext, clock: Clock, settings: MarketIntelligenceSettings | None = None
) -> IntelligenceContext:
    settings = settings or get_market_intelligence_settings()
    service = MarketIntelligenceService(
        market.session_factory,
        market.provider,
        market.settings,
        settings,
        clock,
        ingestion=market.service,
    )
    return IntelligenceContext(settings, market.provider, service, market.session_factory, clock)


def execute_blocking(service: MarketIntelligenceService, run_id: uuid.UUID) -> None:
    """Execute a created run on its own event loop (API background thread)."""
    try:
        asyncio.run(service.execute(run_id))
    except Exception:
        # The failure is recorded on the run; keep the server's worker thread alive.
        logger.exception("intelligence.background_run_failed", run_id=str(run_id))


__all__ = ["IntelligenceContext", "build_intelligence_context", "execute_blocking"]

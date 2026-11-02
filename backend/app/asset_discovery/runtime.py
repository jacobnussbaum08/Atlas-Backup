"""Wiring of the asset discovery components for the API, the CLI and the worker."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.config import AssetDiscoverySettings, get_asset_discovery_settings
from app.asset_discovery.service import AssetDiscoveryService
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.runtime import MarketDataContext

logger = get_logger(__name__)


@dataclass(frozen=True)
class AssetDiscoveryContext:
    settings: AssetDiscoverySettings
    provider: MarketDataProvider
    service: AssetDiscoveryService
    session_factory: sessionmaker[Session]
    clock: Clock


def build_asset_discovery_context(
    market: MarketDataContext, clock: Clock, settings: AssetDiscoverySettings | None = None
) -> AssetDiscoveryContext:
    settings = settings or get_asset_discovery_settings()
    service = AssetDiscoveryService(
        market.session_factory,
        market.provider,
        market.settings,
        settings,
        clock,
        ingestion=market.service,
    )
    return AssetDiscoveryContext(settings, market.provider, service, market.session_factory, clock)


def run_due_blocking(service: AssetDiscoveryService, job_id: uuid.UUID | None = None) -> None:
    """Run a job (and the pipeline jobs it creates) on its own event loop (worker thread)."""
    try:
        service.recover()
        asyncio.run(service.run_due(job_id=job_id))
    except Exception:
        # Failures are recorded on the job; keep the server's worker thread alive.
        logger.exception("discovery.background_run_failed", job_id=str(job_id))

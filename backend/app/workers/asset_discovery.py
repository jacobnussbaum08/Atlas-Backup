"""Asset discovery worker (Phase 3A): ``python -m app.workers.asset_discovery``.

Every ``ASSET_DISCOVERY_POLL_INTERVAL`` seconds one cycle:

1. returns jobs whose lease expired (a crashed worker or API process) to PENDING;
2. creates the periodic jobs that are due: a full directory listing every
   ``ASSET_DISCOVERY_FULL_INTERVAL_HOURS``, an active-only listing every
   ``..._INCREMENTAL_INTERVAL_HOURS``, the qualification pipeline every
   ``..._QUALIFICATION_INTERVAL_HOURS`` and a bounded metadata refresh every
   ``..._REFRESH_INTERVAL_HOURS``. The directory is never re-downloaded continuously;
3. claims and runs due jobs one at a time (bounded concurrency: one provider request in
   flight per worker), including jobs queued through the API.

Several workers may run: claiming uses ``FOR UPDATE SKIP LOCKED`` and at most one job per
kind is active, so they never run the same job or overlapping jobs of a kind.

Disabled by default (``ASSET_DISCOVERY_ENABLED=false``): it then exits 0 at once, because
discovery calls the provider's asset directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from pydantic import ValidationError

from app.asset_discovery.config import AssetDiscoverySettings, get_asset_discovery_settings
from app.asset_discovery.service import AssetDiscoveryService
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_market_data_settings,
    get_settings,
)
from app.core.logging import configure_logging, get_logger
from app.data_engine.errors import MarketDataError
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.registry import create_provider
from app.database.sessions import build_engine, build_session_factory

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_CONFIGURATION = 2


async def run_worker(
    service: AssetDiscoveryService,
    settings: AssetDiscoverySettings,
    stop: asyncio.Event,
    *,
    once: bool = False,
) -> int:
    logger.info("discovery_worker.started", owner=service.owner)
    while not stop.is_set():
        try:
            service.recover()
            created = service.schedule_due()
            if created:
                logger.info("discovery_worker.scheduled", jobs=[j.kind.value for j in created])
            await service.run_due()
        except Exception:
            # Each job records its own failure; a cycle-level error (e.g. the database is
            # down) is logged and the next cycle tries again.
            logger.exception("discovery_worker.cycle_failed")
        if once:
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)
    logger.info("discovery_worker.stopped", owner=service.owner)
    return EXIT_OK


async def _main_async(once: bool, duration: float | None) -> int:
    from app.workers.market_data_stream import _install_signal_handlers

    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=f"{settings.service_name}-discovery",
        environment=settings.environment.value,
    )
    discovery = get_asset_discovery_settings()
    if not discovery.enabled and not once:
        logger.info("discovery_worker.disabled", hint="set ASSET_DISCOVERY_ENABLED=true")
        return EXIT_OK
    market = get_market_data_settings()
    clock = SystemClock()
    provider = create_provider(market, clock)
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    service = AssetDiscoveryService(
        sessions,
        provider,
        market,
        discovery,
        clock,
        ingestion=HistoricalIngestionService(sessions, provider, market, clock),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)
    if duration is not None:
        loop.call_later(duration, stop.set)
    try:
        return await run_worker(service, discovery, stop, once=once)
    finally:
        await provider.aclose()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.asset_discovery",
        description="Run the asset discovery worker (reference data and analysis only; no orders).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit (an explicit operator action: runs even when disabled)",
    )
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args.once, args.duration))
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError, MarketDataError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_CONFIGURATION


__all__ = ["EXIT_CONFIGURATION", "EXIT_OK", "main", "run_worker"]

if __name__ == "__main__":
    raise SystemExit(main())

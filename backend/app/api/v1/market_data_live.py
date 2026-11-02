"""Live market-data updates for dashboard clients (Deliverable 2E): Server-Sent Events.

``GET /api/v1/market-data/live`` streams what the stream worker *stored*: new bar rows
(including revisions), and per asset the latest trade and quote. The browser never talks
to the provider and the API opens no provider connection; see ``live_updates``.

Events: ``hello`` (once), ``bar``, ``trade``, ``quote``, ``resync`` (reload from REST),
``error`` (stored data unavailable). A comment line is sent every
``KEEPALIVE_SECONDS`` so proxies keep the connection open.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.v1.market_data import Context
from app.core.exceptions import DependencyUnavailableError, NotFoundError
from app.data_engine.live_updates import LiveEvent, LiveUpdateHub, TooManyClientsError
from app.data_engine.runtime import MarketDataContext
from app.database.repositories import AssetMappingRepository
from app.database.sessions import session_scope

router = APIRouter(prefix="/market-data", tags=["market-data"])

KEEPALIVE_SECONDS = 15.0


def live_hub(request: Request, ctx: MarketDataContext) -> LiveUpdateHub:
    state = request.app.state
    hub: LiveUpdateHub | None = getattr(state, "live_hub", None)
    if hub is None:
        hub = LiveUpdateHub(ctx.session_factory, feed=ctx.provider.feed, clock=state.clock)
        state.live_hub = hub
    return hub


def _resolve(ctx: MarketDataContext, symbols: list[str] | None) -> dict[uuid.UUID, str]:
    wanted = {s.strip().upper() for s in symbols} if symbols else None
    with session_scope(ctx.session_factory) as session:
        mapped = {
            asset.id: asset.symbol
            for asset, _ in AssetMappingRepository(session).current_assets(ctx.provider.provider_id)
            if wanted is None or asset.symbol in wanted
        }
    if wanted is not None:
        unknown = wanted - set(mapped.values())
        if unknown:
            raise NotFoundError(f"not monitored at this provider: {', '.join(sorted(unknown))}")
    return mapped


@router.get(
    "/live",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def live(
    ctx: Context,
    request: Request,
    symbol: Annotated[list[str] | None, Query(description="Default: all monitored")] = None,
) -> StreamingResponse:
    """Server-Sent Events with market data as it is stored (bars, latest trade and quote)."""
    symbols = await run_in_threadpool(_resolve, ctx, symbol)
    hub = live_hub(request, ctx)
    try:
        subscription = await hub.subscribe(symbols)
    except TooManyClientsError as exc:
        raise DependencyUnavailableError(str(exc)) from None
    hello = LiveEvent(
        "hello",
        {
            "provider": ctx.provider.provider_id,
            "feed": ctx.provider.feed,
            "symbols": sorted(symbols.values()),
            "server_time": request.app.state.clock.now().isoformat(),
            "poll_interval_seconds": hub.poll_interval,
            "note": "Stored data only: bars (all revisions), latest trade and quote per asset.",
        },
    )

    async def events() -> AsyncIterator[str]:
        try:
            yield hello.encode()
            while True:
                try:
                    event = await asyncio.wait_for(
                        subscription.queue.get(), timeout=KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield event.encode()
        finally:
            hub.unsubscribe(subscription)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["live_hub", "router"]

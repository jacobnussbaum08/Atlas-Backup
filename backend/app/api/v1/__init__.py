"""Versioned public API. Feature routers are added here as each phase lands."""

from fastapi import APIRouter

from app.api.v1 import (
    assets,
    discovery,
    market_data,
    market_data_live,
    market_data_overview,
    market_data_recovery,
    market_data_stream,
)

router = APIRouter(prefix="/api/v1")
router.include_router(market_data.router)
router.include_router(market_data_stream.router)
router.include_router(market_data_recovery.router)
router.include_router(market_data_overview.router)
router.include_router(market_data_live.router)
router.include_router(assets.router)
router.include_router(discovery.router)

__all__ = ["router"]

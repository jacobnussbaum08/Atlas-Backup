"""Versioned public API. Feature routers are added here as each phase lands."""

from fastapi import APIRouter

from app.api.v1 import (
    ai,
    assets,
    backtests,
    discovery,
    evidence,
    intelligence,
    market_data,
    market_data_live,
    market_data_overview,
    market_data_recovery,
    market_data_stream,
    ml,
    news,
    notifications,
    orchestration,
    paper_orders,
    portfolio,
    risk,
    strategies,
    system,
    validation,
)

router = APIRouter(prefix="/api/v1")
router.include_router(market_data.router)
router.include_router(market_data_stream.router)
router.include_router(market_data_recovery.router)
router.include_router(market_data_overview.router)
router.include_router(market_data_live.router)
router.include_router(assets.router)
router.include_router(discovery.router)
router.include_router(intelligence.router)
router.include_router(news.router)
router.include_router(strategies.router)
router.include_router(ml.router)
router.include_router(ai.router)
router.include_router(portfolio.portfolio_router)
router.include_router(portfolio.accounting_router)
router.include_router(risk.router)
router.include_router(paper_orders.orders_router)
router.include_router(paper_orders.broker_router)
router.include_router(paper_orders.positions_router)
router.include_router(paper_orders.execution_router)
router.include_router(backtests.router)
router.include_router(validation.router)
router.include_router(evidence.router)
router.include_router(orchestration.router)
router.include_router(notifications.router)
router.include_router(system.router)

__all__ = ["router"]

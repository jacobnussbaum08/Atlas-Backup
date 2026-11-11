"""Backtesting API (Phase 8A). **Simulations over stored historical data only.**

Two operator-gated writes: ``POST /backtests`` creates a run and queues it on the API
process's backtest thread; ``POST /backtests/{run_id}/reproduce`` re-executes a completed
run from its stored configuration. Everything else is GET. The simulated orders, fills and
trades under ``/backtests/{run_id}/...`` belong to a replay: none is, or becomes, an order of
the paper account, and nothing here submits, cancels or executes anything (spec 24.1).
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.v1.market_data import require_operator
from app.backtesting import queries
from app.backtesting.config import BacktestRequest
from app.backtesting.enums import RunStatus
from app.backtesting.runtime import BacktestContext
from app.core.exceptions import DependencyUnavailableError
from app.database.sessions import session_scope

router = APIRouter(prefix="/backtests", tags=["backtests"])
_lock = threading.Lock()


def backtesting(request: Request) -> BacktestContext:
    state = request.app.state
    context: BacktestContext | None = getattr(state, "backtesting", None)
    if context is None:
        factory = getattr(state, "backtesting_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the backtester is not configured on this server")
        with _lock:
            context = getattr(state, "backtesting", None)
            if context is None:
                context = factory()
                state.backtesting = context
    return context


Context = Annotated[BacktestContext, Depends(backtesting)]


@router.get("/options")
def options(ctx: Context) -> dict[str, Any]:
    """Strategies (latest stored versions), assets with stored daily bars, timeframes and
    universe layers a backtest can be configured with."""
    with session_scope(ctx.session_factory) as session:
        return queries.options(session, ctx.registry, provider=ctx.provider, feed=ctx.feed)


@router.get("")
def list_runs(
    ctx: Context,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        return queries.runs(session, limit=limit, status=status_filter)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_operator)],
)
def create_run(body: BacktestRequest, ctx: Context) -> dict[str, Any]:
    """Resolve and store a backtest run, then replay it on the backtest thread."""
    run_id = ctx.service.create(body, actor="operator:api")
    ctx.submit(run_id)
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, run_id)


@router.get("/compare")
def compare(
    ctx: Context, run_ids: Annotated[list[uuid.UUID], Query(min_length=1, max_length=8)]
) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.compare(session, run_ids)


@router.get("/{run_id}")
def run_detail(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, run_id)


@router.post(
    "/{run_id}/reproduce",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_operator)],
)
def reproduce(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    """Re-execute a completed run from its stored configuration as a new run; the new run
    records whether its result hash equals the original's."""
    new_id = ctx.service.prepare_reproduction(run_id, actor="operator:api")
    ctx.submit(new_id)
    with session_scope(ctx.session_factory) as session:
        return queries.run_detail(session, new_id)


def _get(run_id: uuid.UUID, ctx: BacktestContext, fn: Any, **kw: Any) -> Any:
    with session_scope(ctx.session_factory) as session:
        return fn(session, run_id, **kw)


@router.get("/{run_id}/progress")
def progress(run_id: uuid.UUID, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        detail = queries.run_summary(queries._run(session, run_id))
        cursors = queries.cursors(session, run_id)
    return {"run": detail, "cursors": cursors[-50:]}


@router.get("/{run_id}/equity")
def equity(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.equity)  # type: ignore[no-any-return]


@router.get("/{run_id}/metrics")
def metrics(run_id: uuid.UUID, ctx: Context, scope: str | None = None) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.metrics, scope=scope)  # type: ignore[no-any-return]


@router.get("/{run_id}/benchmarks")
def benchmarks(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.benchmarks)  # type: ignore[no-any-return]


@router.get("/{run_id}/trades")
def trades(
    run_id: uuid.UUID, ctx: Context, strategy_id: str | None = None, symbol: str | None = None
) -> list[dict[str, Any]]:
    return _get(  # type: ignore[no-any-return]
        run_id, ctx, queries.trades, strategy_id=strategy_id, symbol=symbol
    )


@router.get("/{run_id}/trades/{seq}")
def trade_detail(run_id: uuid.UUID, seq: int, ctx: Context) -> dict[str, Any]:
    with session_scope(ctx.session_factory) as session:
        return queries.trade_detail(session, run_id, seq)


@router.get("/{run_id}/orders")
def orders(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.orders)  # type: ignore[no-any-return]


@router.get("/{run_id}/fills")
def fills(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.fills)  # type: ignore[no-any-return]


@router.get("/{run_id}/proposals")
def proposals(
    run_id: uuid.UUID, ctx: Context, strategy_id: str | None = None, outcome: str | None = None
) -> list[dict[str, Any]]:
    return _get(  # type: ignore[no-any-return]
        run_id, ctx, queries.proposals, strategy_id=strategy_id, outcome=outcome
    )


@router.get("/{run_id}/rejections")
def rejections(run_id: uuid.UUID, ctx: Context, stage: str | None = None) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.rejections, stage=stage)  # type: ignore[no-any-return]


@router.get("/{run_id}/events")
def events(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.events)  # type: ignore[no-any-return]


@router.get("/{run_id}/errors")
def errors(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.errors)  # type: ignore[no-any-return]


@router.get("/{run_id}/evaluations")
def evaluations(run_id: uuid.UUID, ctx: Context) -> list[dict[str, Any]]:
    return _get(run_id, ctx, queries.evaluations)  # type: ignore[no-any-return]


@router.get("/{run_id}/journal")
def journal(
    run_id: uuid.UUID,
    ctx: Context,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[dict[str, Any]]:
    return _get(  # type: ignore[no-any-return]
        run_id, ctx, queries.journal, offset=offset, limit=limit
    )


__all__ = ["router"]

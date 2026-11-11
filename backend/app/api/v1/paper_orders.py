"""Paper Orders, Positions and Execution API (Phases 7A-7B). **GET only. Simulated only.**

Order history, details, status, events, open / cancelled / rejected orders, reservation
linkage, refused submission requests and the simulated paper broker's status. Nothing
here submits, cancels, fills or approves an order: submission is a service interface of
the execution coordinator, cancellation and controlled test events are operator CLI
actions. There is no brokerage connection behind any of it (spec 24.1).

Phase 7B adds the simulated positions (open and closed, with their events, orders, fills
and close requests), the execution analytics (simulated slippage, fill rates, partial
fills, costs, model assumptions) and the execution-model versions. Closing a position or
tightening its protection is an operator CLI action; nothing here changes anything.

Without ``account_id`` the primary paper account is used.
"""

from __future__ import annotations

import threading
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.core.exceptions import DependencyUnavailableError
from app.database.sessions import session_scope
from app.execution import position_queries, queries
from app.execution.enums import ExecutionDecision, OrderEventType, OrderStatus, PositionStatus
from app.execution.runtime import ExecutionContext
from app.portfolio.queries import resolve_account

orders_router = APIRouter(prefix="/paper-orders", tags=["paper-orders"])
broker_router = APIRouter(prefix="/paper-broker", tags=["paper-broker"])
positions_router = APIRouter(prefix="/paper-positions", tags=["paper-positions"])
execution_router = APIRouter(prefix="/paper-execution", tags=["paper-execution"])
_lock = threading.Lock()


def execution(request: Request) -> ExecutionContext:
    """The execution context, built on first use."""
    state = request.app.state
    context: ExecutionContext | None = getattr(state, "execution", None)
    if context is None:
        factory = getattr(state, "execution_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the paper execution engine is not configured")
        with _lock:
            context = getattr(state, "execution", None)
            if context is None:
                context = factory()
                state.execution = context
    return context


Context = Annotated[ExecutionContext, Depends(execution)]
AccountId = Annotated[uuid.UUID | None, Query(description="Defaults to the primary account")]
Limit = Annotated[int, Query(ge=1, le=500)]
Group = Literal[
    "open", "closed", "filled", "partially_filled", "cancelled", "rejected", "expired", "failed"
]
PositionFilter = Literal["open", "closed"]


@orders_router.get("")
def list_orders(
    ctx: Context,
    account_id: AccountId = None,
    status: OrderStatus | None = None,
    group: Group | None = None,
    limit: Limit = 100,
) -> list[dict[str, Any]]:
    """Simulated paper orders, newest first (``group=open|cancelled|rejected|...``)."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return queries.orders(session, account.id, limit=limit, status=status, group=group)


@orders_router.get("/open")
def open_orders(
    ctx: Context, account_id: AccountId = None, limit: Limit = 200
) -> list[dict[str, Any]]:
    """Orders the paper broker is (or is about to be) working."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return queries.orders(session, account.id, limit=limit, group="open")


@orders_router.get("/summary")
def orders_summary(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Counts by status, open orders and refused submission requests."""
    with session_scope(ctx.session_factory) as session:
        return queries.summary(session, resolve_account(session, account_id).id)


@orders_router.get("/events")
def order_events(
    ctx: Context,
    account_id: AccountId = None,
    event_type: OrderEventType | None = None,
    limit: Limit = 100,
) -> list[dict[str, Any]]:
    """Order state changes, newest first (timestamp, source, actor and reason each)."""
    with session_scope(ctx.session_factory) as session:
        return queries.events(
            session, resolve_account(session, account_id).id, limit=limit, event_type=event_type
        )


@orders_router.get("/requests")
def execution_requests(
    ctx: Context,
    account_id: AccountId = None,
    decision: ExecutionDecision | None = None,
    limit: Limit = 100,
) -> list[dict[str, Any]]:
    """Submission requests (``decision=REFUSED``: unauthorized or invalid, with reasons)."""
    with session_scope(ctx.session_factory) as session:
        return queries.execution_requests(
            session, resolve_account(session, account_id).id, limit=limit, decision=decision
        )


@orders_router.get("/{order_id}")
def get_order(ctx: Context, order_id: uuid.UUID) -> dict[str, Any]:
    """One order: its events, fills, reservation linkage, request, broker book, outbox."""
    with session_scope(ctx.session_factory) as session:
        return queries.order(session, order_id)


@orders_router.get("/{order_id}/events")
def get_order_events(ctx: Context, order_id: uuid.UUID) -> list[dict[str, Any]]:
    with session_scope(ctx.session_factory) as session:
        detail = queries.order(session, order_id)
        events: list[dict[str, Any]] = detail["events"]
        return events


@broker_router.get("/status")
def paper_broker_status(ctx: Context) -> dict[str, Any]:
    """The simulated paper broker: run state, fill model, working orders, outbox backlog."""
    with session_scope(ctx.session_factory) as session:
        return queries.broker_status(session, ctx.broker)


@positions_router.get("")
def list_positions(
    ctx: Context,
    account_id: AccountId = None,
    status: PositionFilter | None = None,
    limit: Limit = 200,
) -> list[dict[str, Any]]:
    """Simulated positions (``status=open|closed``), newest first."""
    with session_scope(ctx.session_factory) as session:
        account = resolve_account(session, account_id)
        return position_queries.positions(
            session,
            account.id,
            status=None if status is None else PositionStatus(status.upper()),
            limit=limit,
            now=ctx.clock.now(),
        )


@positions_router.get("/summary")
def positions_summary(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Open and closed counts, unrealized and realized P/L, exit reasons."""
    with session_scope(ctx.session_factory) as session:
        return position_queries.position_summary(session, resolve_account(session, account_id).id)


@positions_router.get("/{position_id}")
def get_position(ctx: Context, position_id: uuid.UUID) -> dict[str, Any]:
    """One position: its events, its entry and exit orders, fills with breakdowns."""
    with session_scope(ctx.session_factory) as session:
        return position_queries.position(session, position_id, now=ctx.clock.now())


@execution_router.get("/analytics")
def execution_analytics(ctx: Context, account_id: AccountId = None) -> dict[str, Any]:
    """Simulated slippage, fill rates, partial fills, costs and the model assumptions."""
    with session_scope(ctx.session_factory) as session:
        return position_queries.analytics(session, resolve_account(session, account_id).id)


@execution_router.get("/models")
def execution_models(ctx: Context) -> list[dict[str, Any]]:
    """Every execution-model version simulated fills were derived under."""
    with session_scope(ctx.session_factory) as session:
        return position_queries.models(session)


__all__ = ["broker_router", "execution_router", "orders_router", "positions_router"]

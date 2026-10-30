"""Liveness (``/health``) and readiness (``/ready``) endpoints.

* ``/health`` answers whether the API process is alive. It touches no
  dependencies, so orchestrators do not restart the API because Redis blinked.
* ``/ready`` answers whether Atlas can serve traffic: PostgreSQL reachable,
  schema at the expected migration head, and Redis reachable. Any failure
  returns HTTP 503.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.core.security import TRADING_MODE, TradingMode
from app.monitoring.health import ComponentHealth, HealthStatus

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str
    trading_mode: TradingMode
    time: datetime
    uptime_seconds: float


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    trading_mode: TradingMode
    checked_at: datetime
    components: list[ComponentHealth]


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    now = state.clock.now()
    return HealthResponse(
        status="ok",
        service=state.settings.service_name,
        version=state.version,
        environment=state.settings.environment.value,
        trading_mode=TRADING_MODE,
        time=now,
        uptime_seconds=round((now - state.started_at).total_seconds(), 3),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
def ready(request: Request, response: Response) -> ReadinessResponse:
    state = request.app.state
    components = [check() for check in state.health_checks.values()]
    is_ready = bool(components) and all(c.status is HealthStatus.OK for c in components)
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        trading_mode=TRADING_MODE,
        checked_at=state.clock.now(),
        components=components,
    )

"""System status API (Phase 9 dashboard). GET only.

``/system/workers``: what each background worker last recorded in PostgreSQL (heartbeat or
newest output), with a derived health that never claims a worker is running without a
current heartbeat. See :mod:`app.monitoring.workers`.

Nothing here starts, stops, configures or controls a worker, and no credential or setting
value other than on/off switches is reported.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.core.exceptions import DependencyUnavailableError
from app.database.sessions import session_scope
from app.monitoring.workers import WorkersReport, load_switches, workers_report

router = APIRouter(prefix="/system", tags=["system"])


def _session_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise DependencyUnavailableError("the database is not configured")
    return factory


@router.get("/workers", response_model=WorkersReport)
def workers(request: Request) -> WorkersReport:
    factory = _session_factory(request)
    now = request.app.state.clock.now()
    with session_scope(factory) as session:
        return workers_report(session, now, load_switches())

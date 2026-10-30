"""Dependency health checks used by the readiness endpoint.

Each check returns a :class:`ComponentHealth` and never raises: failures are
reported with a redacted, single-line detail so that credentials and internal
stack traces never reach API clients.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel
from sqlalchemy import Engine, text

from app.core.logging import get_logger
from app.core.security.redaction import redact_text

logger = get_logger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


class HealthStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ComponentHealth(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


class SupportsPing(Protocol):
    def ping(self) -> object: ...


HealthCheck = Callable[[], ComponentHealth]


def _failure(name: str, started: float, exc: BaseException) -> ComponentHealth:
    message = redact_text(str(exc).strip().splitlines()[0] if str(exc).strip() else "")
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    logger.warning("health.check_failed", component=name, detail=detail)
    return ComponentHealth(
        name=name,
        status=HealthStatus.ERROR,
        latency_ms=_elapsed_ms(started),
        detail=detail[:300],
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def expected_migration_heads(alembic_ini: Path = ALEMBIC_INI) -> frozenset[str]:
    """Revision heads shipped with this code base."""
    config = Config(str(alembic_ini))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def check_database(engine: Engine) -> ComponentHealth:
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return _failure("database", started, exc)
    return ComponentHealth(name="database", status=HealthStatus.OK, latency_ms=_elapsed_ms(started))


def check_migrations(engine: Engine, expected_heads: frozenset[str]) -> ComponentHealth:
    """Ready only when the database schema is exactly at the code's migration head."""
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            applied = frozenset(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    except Exception as exc:
        return _failure("migrations", started, exc)
    if applied != expected_heads:
        return ComponentHealth(
            name="migrations",
            status=HealthStatus.ERROR,
            latency_ms=_elapsed_ms(started),
            detail=(
                f"schema at {sorted(applied) or 'no revision'}, expected {sorted(expected_heads)}"
            ),
        )
    return ComponentHealth(
        name="migrations",
        status=HealthStatus.OK,
        latency_ms=_elapsed_ms(started),
        detail=f"at head {sorted(applied)}",
    )


def check_redis(client: SupportsPing) -> ComponentHealth:
    started = time.perf_counter()
    try:
        if client.ping() is not True:
            raise ConnectionError("unexpected PING reply")
    except Exception as exc:
        return _failure("redis", started, exc)
    return ComponentHealth(name="redis", status=HealthStatus.OK, latency_ms=_elapsed_ms(started))

"""Wiring of the Strategy Laboratory for the API, the CLI and the evaluation worker."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.strategies.config import StrategySettings, get_strategy_settings
from app.strategies.engine import StrategyEngine
from app.strategies.registry import StrategyRegistry, default_registry


@dataclass(frozen=True)
class StrategyContext:
    settings: StrategySettings
    registry: StrategyRegistry
    engine: StrategyEngine
    session_factory: sessionmaker[Session]
    clock: Clock


def build_strategy_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    provider: str,
    feed: str,
    settings: StrategySettings | None = None,
    registry: StrategyRegistry | None = None,
    worker_id: str | None = None,
) -> StrategyContext:
    settings = settings or get_strategy_settings()
    registry = registry or default_registry()
    engine = StrategyEngine(
        session_factory,
        registry,
        settings,
        clock,
        provider=provider,
        feed=feed,
        worker_id=worker_id,
    )
    return StrategyContext(settings, registry, engine, session_factory, clock)


__all__ = ["StrategyContext", "build_strategy_context"]

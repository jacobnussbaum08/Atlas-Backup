"""Wiring of the ML engine for the API and the CLI (Phase 5A)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import CALENDARS, ExchangeCalendar
from app.core.clocks import Clock
from app.ml_engine.config import MLSettings, get_ml_settings
from app.ml_engine.inference.engine import InferenceEngine
from app.ml_engine.training.trainer import Trainer

CALENDAR = "XNYS"


@dataclass(frozen=True)
class MLContext:
    settings: MLSettings
    trainer: Trainer
    inference: InferenceEngine
    session_factory: sessionmaker[Session]
    clock: Clock
    provider: str
    feed: str
    calendar: ExchangeCalendar


def build_ml_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    provider: str,
    feed: str,
    settings: MLSettings | None = None,
) -> MLContext:
    settings = settings or get_ml_settings()
    return MLContext(
        settings=settings,
        trainer=Trainer(session_factory, settings, clock),
        inference=InferenceEngine(session_factory, settings, clock),
        session_factory=session_factory,
        clock=clock,
        provider=provider,
        feed=feed,
        calendar=CALENDARS[CALENDAR],
    )


__all__ = ["CALENDAR", "MLContext", "build_ml_context"]

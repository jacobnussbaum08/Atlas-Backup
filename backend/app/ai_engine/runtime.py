"""Wiring of the AI decision engine for the API, the CLI and the workers (Phase 5B)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.ai_engine.config import AISettings, get_ai_settings
from app.ai_engine.coordinator.policy import DecisionPolicy
from app.ai_engine.coordinator.service import Coordinator
from app.asset_discovery.sessions import CALENDARS, ExchangeCalendar
from app.core.clocks import Clock

CALENDAR = "XNYS"


def load_policy(path: Path | None) -> DecisionPolicy:
    """The policy in ``path`` (JSON), or the documented default."""
    if path is None:
        return DecisionPolicy()
    return DecisionPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class AIContext:
    settings: AISettings
    coordinator: Coordinator
    policy: DecisionPolicy
    session_factory: sessionmaker[Session]
    clock: Clock
    calendar: ExchangeCalendar


def build_ai_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    inference: object | None = None,
    settings: AISettings | None = None,
    worker_id: str = "coordinator",
) -> AIContext:
    settings = settings or get_ai_settings()
    calendar = CALENDARS[CALENDAR]
    return AIContext(
        settings=settings,
        coordinator=Coordinator(
            session_factory, clock, inference=inference, calendar=calendar, worker_id=worker_id
        ),
        policy=load_policy(settings.decision_policy_file),
        session_factory=session_factory,
        clock=clock,
        calendar=calendar,
    )


__all__ = ["CALENDAR", "AIContext", "build_ai_context", "load_policy"]

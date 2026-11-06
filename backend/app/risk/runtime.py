"""Wiring of the risk service for the API and the CLI (Phase 6B)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings, get_accounting_settings
from app.core.clocks import Clock
from app.risk.service import RiskService


@dataclass(frozen=True)
class RiskContext:
    session_factory: sessionmaker[Session]
    clock: Clock
    accounting: AccountingSettings
    service: RiskService


def build_risk_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    accounting: AccountingSettings | None = None,
) -> RiskContext:
    accounting = accounting or get_accounting_settings()
    return RiskContext(
        session_factory=session_factory,
        clock=clock,
        accounting=accounting,
        service=RiskService(session_factory, clock, accounting),
    )


__all__ = ["RiskContext", "build_risk_context"]

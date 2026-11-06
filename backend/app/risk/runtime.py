"""Wiring of the risk and reservation services for the API and the CLI (Phases 6B, 6C)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings, get_accounting_settings
from app.core.clocks import Clock
from app.risk.reservations.reconciliation import ReservationReconciler
from app.risk.reservations.service import ReservationService
from app.risk.service import RiskService


@dataclass(frozen=True)
class RiskContext:
    session_factory: sessionmaker[Session]
    clock: Clock
    accounting: AccountingSettings
    service: RiskService
    reservations: ReservationService | None = None
    reconciler: ReservationReconciler | None = None


def build_risk_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    accounting: AccountingSettings | None = None,
) -> RiskContext:
    accounting = accounting or get_accounting_settings()
    service = RiskService(session_factory, clock, accounting)
    return RiskContext(
        session_factory=session_factory,
        clock=clock,
        accounting=accounting,
        service=service,
        reservations=ReservationService(session_factory, clock, accounting, service),
        reconciler=ReservationReconciler(session_factory, clock),
    )


__all__ = ["RiskContext", "build_risk_context"]

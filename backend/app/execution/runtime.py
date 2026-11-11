"""Wiring of the paper execution engine for the API, the CLI and the workers (7A-7B).

There is exactly one broker implementation to wire: the simulated
:class:`~app.paper_broker.controlled.ControlledPaperBroker`, with the execution simulator
and the position monitor (Phase 7B). There is no setting, flag or environment variable that
selects another broker or enables live trading.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings, get_accounting_settings
from app.accounting.posting import PostingService
from app.core.clocks import Clock
from app.execution.models import ExecutionConfig
from app.execution.monitor import MonitorConfig, PositionMonitor
from app.execution.portfolio import AccountingPositionLedger
from app.execution.reconciliation import ExecutionReconciler
from app.execution.service import ExecutionService
from app.execution.simulation import ExecutionSimulator
from app.paper_broker.controlled import ControlledPaperBroker
from app.paper_broker.execution_model import DEFAULT_MODEL, ExecutionModel
from app.risk.reservations.service import ReservationService
from app.risk.service import RiskService


@dataclass(frozen=True)
class ExecutionContext:
    session_factory: sessionmaker[Session]
    clock: Clock
    broker: ControlledPaperBroker
    service: ExecutionService
    reconciler: ExecutionReconciler
    simulator: ExecutionSimulator
    monitor: PositionMonitor


def build_execution_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    accounting: AccountingSettings | None = None,
    config: ExecutionConfig | None = None,
    model: ExecutionModel = DEFAULT_MODEL,
    monitor: MonitorConfig | None = None,
) -> ExecutionContext:
    accounting = accounting or get_accounting_settings()
    risk = RiskService(session_factory, clock, accounting)
    reservations = ReservationService(session_factory, clock, accounting, risk)
    broker = ControlledPaperBroker()
    posting = PostingService(session_factory, clock, accounting)
    service = ExecutionService(
        session_factory,
        clock,
        accounting,
        broker=broker,
        reservations=reservations,
        ledger=AccountingPositionLedger(posting),
        config=config,
    )
    return ExecutionContext(
        session_factory=session_factory,
        clock=clock,
        broker=broker,
        service=service,
        reconciler=ExecutionReconciler(session_factory, clock),
        simulator=ExecutionSimulator(service, clock, model),
        monitor=PositionMonitor(
            service, clock, model, accounting=accounting, posting=posting, config=monitor
        ),
    )


__all__ = ["ExecutionContext", "build_execution_context"]

"""Wiring of the trading orchestrator for the worker, the CLI and the API (Phase 10A).

Paper trading only. The orchestrator drives the existing engines through their public
services: the strategy evaluation cycle (4A/4B), the AI coordinator (5B), the risk engine
and its reservations (6B/6C), the paper execution engine (7A) and the reconciliations
(6A/6C/7A). There is exactly one execution path, the simulated paper broker of 7A/7B, and no
setting here can select another or enable live trading.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings, get_accounting_settings
from app.accounting.reconciliation import ReconciliationService
from app.accounting.runtime import build_accounting_context
from app.core.clocks import Clock
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import PortfolioAccount
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.execution.runtime import ExecutionContext, build_execution_context
from app.orchestration.config import OrchestratorSettings
from app.orchestration.intents import IntentHandler
from app.orchestration.operations import OperationsWork
from app.orchestration.orchestrator import SYSTEM_ACTOR, Orchestrator, Stages
from app.orchestration.protection import ProtectionGuard
from app.orchestration.service import ControlService
from app.portfolio.queries import resolve_account
from app.risk.enums import Severity
from app.risk.runtime import RiskContext, build_risk_context

logger = get_logger(__name__)


@dataclass(frozen=True)
class ControlContext:
    """What the API and the CLI need to read and change the trading controls."""

    session_factory: sessionmaker[Session]
    clock: Clock
    controls: ControlService
    risk: RiskContext
    execution: ExecutionContext


def build_control_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    accounting: AccountingSettings | None = None,
) -> ControlContext:
    accounting = accounting or get_accounting_settings()
    risk = build_risk_context(session_factory, clock, accounting=accounting)
    execution = build_execution_context(session_factory, clock, accounting=accounting)
    return ControlContext(
        session_factory=session_factory,
        clock=clock,
        controls=ControlService(
            session_factory, clock, risk=risk.service, execution=execution.service
        ),
        risk=risk,
        execution=execution,
    )


def resolve_orchestrated_account(session: Session, name: str) -> PortfolioAccount:
    """The account named ``name``; the primary paper account when ``name`` is empty."""
    if not name.strip():
        return resolve_account(session, None)
    found = session.scalar(select(PortfolioAccount).where(PortfolioAccount.name == name.strip()))
    if found is None:
        raise NotFoundError(f"no portfolio account named {name.strip()!r}")
    return found


def report_finding(
    session_factory: sessionmaker[Session],
    clock: Clock,
    account_id: uuid.UUID,
    *,
    event_type: str,
    subject_kind: str,
    subject_id: str | None,
    summary: str,
    details: dict[str, Any],
    severity: Severity = Severity.HIGH,
) -> None:
    with session_scope(session_factory) as session:
        session.add(
            SystemHealthEventRecord(
                component="orchestration",
                event_type=event_type,
                severity=severity,
                portfolio_account_id=account_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                summary=summary[:500],
                details=details,
                occurred_at=clock.now(),
            )
        )


# ---------------------------------------------------------------------- scheduled work
class ScheduledWork:
    """The scheduled jobs' bodies. Each returns a JSON-able summary; a failed
    reconciliation is a *finding*, not a job error: the job succeeds, the result says
    ``passed: false`` and a HIGH ``system_health_events`` row is written (the execution
    reconciler writes its own CRITICAL row)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        account_id: uuid.UUID,
        *,
        risk: RiskContext,
        execution: ExecutionContext,
        accounting: ReconciliationService,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._account = account_id
        self._risk = risk
        self._execution = execution
        self._accounting = accounting

    def risk_monitor(self) -> dict[str, Any]:
        state = self._risk.service.monitor(self._account)
        return {
            "snapshot_id": str(state.snapshot_id),
            "mark_status": state.view.mark_status.value,
            "transitions": [f"{t.breaker.value} {t.event.value}" for t in state.transitions],
        }

    def expire_reservations(self) -> dict[str, Any]:
        assert self._risk.reservations is not None  # noqa: S101  # built by build_risk_context
        summary = self._risk.reservations.expire_due(self._account)
        return {"summary": str(summary)[:500]}

    def reservation_reconcile(self) -> dict[str, Any]:
        assert self._risk.reconciler is not None  # noqa: S101  # built by build_risk_context
        result = self._risk.reconciler.run(self._account, actor=SYSTEM_ACTOR)
        passed = not result.unexplained
        if not passed:
            report_finding(
                self._factory,
                self._clock,
                self._account,
                event_type="RESERVATION_RECONCILIATION_FAILED",
                subject_kind="risk_reservation_reconciliation",
                subject_id=str(result.run_id),
                summary=f"{len(result.unexplained)} unexplained reservation difference(s)",
                details={"unexplained": result.unexplained[:50]},
            )
        return {
            "run_id": str(result.run_id),
            "status": result.status.value,
            "passed": passed,
            "warnings": len(result.warnings),
        }

    def execution_reconcile(self) -> dict[str, Any]:
        result = self._execution.reconciler.run(self._account)
        return {
            "passed": result.passed,
            "orders_checked": result.orders_checked,
            "positions_checked": result.positions_checked,
            "findings": len(result.findings),
            "warnings": len(result.warnings),
        }

    def accounting_reconcile(self) -> dict[str, Any]:
        result = self._accounting.run(self._account, actor=SYSTEM_ACTOR, trigger="SCHEDULED")
        passed = result.checks_failed == 0
        if not passed:
            report_finding(
                self._factory,
                self._clock,
                self._account,
                event_type="ACCOUNTING_RECONCILIATION_FAILED",
                subject_kind="reconciliation_run",
                subject_id=str(result.run_id),
                summary=f"{result.checks_failed} of {result.checks_total} ledger check(s) failed",
                details={"failed": [f.code for f in result.failures][:50]},
                severity=Severity.CRITICAL,
            )
        return {
            "run_id": str(result.run_id),
            "status": result.status.value,
            "passed": passed,
            "checks_failed": result.checks_failed,
        }


# ---------------------------------------------------------------------- the orchestrator
@dataclass
class OrchestratorRuntime:
    orchestrator: Orchestrator
    account_id: uuid.UUID
    account_name: str


def _research_stages(
    session_factory: sessionmaker[Session],
    clock: Clock,
    settings: OrchestratorSettings,
    worker_id: str,
) -> dict[str, Any]:
    """Strategy cycle, decision run, market intelligence and decision outcomes. They need
    the market-data provider configuration (read-only provider; no orders)."""
    needed = (
        settings.run_strategy_stage
        or settings.run_decision_stage
        or settings.run_market_intelligence
        or settings.run_decision_outcomes
    )
    if not needed:
        return {}
    from app.ai_engine.coordinator.baselines import evaluate_decisions
    from app.ai_engine.enums import DecisionMode
    from app.ai_engine.runtime import build_ai_context
    from app.data_engine.runtime import build_market_data_context
    from app.market_intelligence.enums import RunKind, RunStatus
    from app.market_intelligence.runtime import build_intelligence_context
    from app.ml_engine.runtime import build_ml_context
    from app.strategies.config import get_strategy_settings
    from app.strategies.runtime import build_strategy_context
    from app.workers.strategy_evaluation import StrategyEvaluationWorker

    market = build_market_data_context(session_factory, clock)
    provider, feed = market.provider.provider_id, market.provider.feed
    stages: dict[str, Any] = {}
    if settings.run_strategy_stage:
        strategy = StrategyEvaluationWorker(
            build_strategy_context(session_factory, clock, provider=provider, feed=feed), clock
        )
        strategy.sync()  # register the catalog once, as the strategy worker does at start
        stages["strategy_cycle"] = strategy.cycle
    ai = None
    if settings.run_decision_stage or settings.run_decision_outcomes:
        inference = None
        if settings.run_decision_stage:
            inference = build_ml_context(
                session_factory, clock, provider=provider, feed=feed
            ).inference
        ai = build_ai_context(session_factory, clock, inference=inference, worker_id=worker_id)
    if settings.run_decision_stage and ai is not None:
        context = ai

        def decide() -> Any:
            return context.coordinator.run(
                context.policy, mode=DecisionMode.LIVE, trigger="orchestrator", actor=worker_id
            )

        stages["decide"] = decide
    if settings.run_market_intelligence:
        intelligence = build_intelligence_context(market, clock)

        def market_intelligence() -> dict[str, Any]:
            run = asyncio.run(intelligence.service.run(RunKind.LIVE, requested_by=SYSTEM_ACTOR))
            if run.status is RunStatus.FAILED:
                raise RuntimeError(f"market intelligence run {run.id} FAILED: {run.error}")
            return {"run_id": str(run.id), "status": run.status.value}

        stages["market_intelligence"] = market_intelligence
    if settings.run_decision_outcomes and ai is not None:
        context = ai
        settle = get_strategy_settings().settle_seconds

        def decision_outcomes() -> dict[str, Any]:
            now = clock.now()
            with session_scope(session_factory) as session:
                out = evaluate_decisions(
                    session,
                    calendar=context.calendar,
                    known_at=now,
                    start=now - timedelta(days=settings.decision_outcome_lookback_days),
                    cost_bps=context.policy.assumed_round_trip_cost_bps,
                    settle_seconds=settle,
                    actor=SYSTEM_ACTOR,
                )
            return {k: v for k, v in out.items() if isinstance(v, (str, int, float, bool))}

        stages["decision_outcomes"] = decision_outcomes
    return stages


def _learning_stages(
    session_factory: sessionmaker[Session], clock: Clock, settings: OrchestratorSettings
) -> dict[str, Any]:
    """The 8B degradation monitor (no provider needed). It only warns unless an operator
    approved a SUSPEND rule; it never enables, approves or changes a risk limit."""
    if not settings.run_degradation_monitor:
        return {}
    from app.learning.monitoring import MonitoringService
    from app.strategies.registry import default_registry

    monitoring = MonitoringService(session_factory, clock, default_registry())

    def degradation_monitor() -> dict[str, Any]:
        run_id, report = monitoring.run()
        subjects = report.get("subjects", [])
        return {
            "monitor_run_id": str(run_id),
            "subjects": len(subjects),
            "alerts": sum(len(s.get("alerts") or []) for s in subjects),
        }

    return {"degradation_monitor": degradation_monitor}


def execution_pass(execution: ExecutionContext, *, batch: int = 100) -> dict[str, Any]:
    """One pass of the paper execution worker's loop **without** its heartbeat: deliver due
    outbox events, simulate working orders, expire due orders. Safe alongside the worker
    (SKIP LOCKED claims, idempotent handlers). The orchestrator's backstop (D-214)."""
    dispatcher = execution.service.dispatcher()
    first = dispatcher.dispatch(limit=batch)
    simulated = execution.simulator.run_once()
    second = dispatcher.dispatch(limit=batch) if simulated.fills or simulated.triggers else None
    expired = execution.service.expire_due()
    return {
        "delivered": first.delivered + (second.delivered if second else 0),
        "dead": first.dead + (second.dead if second else 0),
        "fills": simulated.fills,
        "triggers": simulated.triggers,
        "expired": len(expired.expired),
    }


def build_orchestrator(
    session_factory: sessionmaker[Session],
    clock: Clock,
    settings: OrchestratorSettings,
    *,
    worker_id: str,
    accounting: AccountingSettings | None = None,
    research: dict[str, Any] | None = None,
) -> OrchestratorRuntime:
    """The orchestrator for the configured account. ``research`` replaces the research
    stages (tests pass stubs; None builds the real ones from the environment)."""
    accounting = accounting or get_accounting_settings()
    with session_scope(session_factory) as session:
        account = resolve_orchestrated_account(session, settings.account_name)
        account_id, account_name = account.id, account.name
    risk = build_risk_context(session_factory, clock, accounting=accounting)
    execution = build_execution_context(session_factory, clock, accounting=accounting)
    assert risk.reservations is not None  # noqa: S101  # built by build_risk_context
    valuation = build_accounting_context(session_factory, clock, settings=accounting).valuation
    work = ScheduledWork(
        session_factory,
        clock,
        account_id,
        risk=risk,
        execution=execution,
        accounting=ReconciliationService(session_factory, clock),
    )
    operations = OperationsWork(
        session_factory,
        clock,
        account_id,
        accounting,
        require_workers=settings.require_protection_workers,
        checkpoint=lambda: valuation.checkpoint(account_id, actor=SYSTEM_ACTOR),
    )
    protection = ProtectionGuard(
        session_factory,
        clock,
        account_id,
        require_workers=settings.require_protection_workers,
        monitor_pass=execution.monitor.run_once if settings.backstop_position_monitor else None,
        execution_pass=(lambda: execution_pass(execution))
        if settings.backstop_paper_execution
        else None,
    )
    if research is None:
        research = {
            **_research_stages(session_factory, clock, settings, worker_id),
            **_learning_stages(session_factory, clock, settings),
        }
    stages = Stages(
        risk_monitor=work.risk_monitor,
        expire_reservations=work.expire_reservations,
        reservation_reconcile=work.reservation_reconcile,
        execution_reconcile=work.execution_reconcile,
        accounting_reconcile=work.accounting_reconcile,
        data_freshness=operations.data_freshness,
        session_open_check=operations.session_open_check,
        session_close_review=operations.session_close_review,
        **research,
    )
    intents = IntentHandler(
        clock,
        risk.reservations,
        execution.service,
        entry_order_type=settings.entry_order_type,
        gate=protection.gate if settings.require_protection_workers else None,
    )
    orchestrator = Orchestrator(
        session_factory,
        clock,
        settings,
        account_id=account_id,
        intents=intents,
        stages=stages,
        worker_id=worker_id,
        protection=protection,
    )
    logger.info(
        "orchestration.built",
        account=account_name,
        stages=[name for name, fn in vars(stages).items() if fn is not None],
        simulated=True,
    )
    return OrchestratorRuntime(orchestrator, account_id, account_name)


__all__ = [
    "ControlContext",
    "OrchestratorRuntime",
    "ScheduledWork",
    "build_control_context",
    "build_orchestrator",
    "execution_pass",
    "report_finding",
    "resolve_orchestrated_account",
]

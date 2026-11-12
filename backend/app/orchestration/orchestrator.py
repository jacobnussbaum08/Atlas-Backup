"""The trading orchestrator's tick (Phases 10A-10B).

One tick, in order (each step idempotent; a failing step is recorded and the next tick
retries it)::

    heartbeat (orchestrator_status)
    expire due reservations (6C)                       housekeeping, any mode
    protection (10B): are the position monitor and the paper execution worker running?
                    alert; backstop them; open or close the entry gate
    strategy stage: one strategy-evaluation cycle (4A/4B)          proposals
    decision stage: a LIVE coordinator run (5B), only when new LIVE proposals arrived
                    since the last LIVE run and not more often than configured
    intake: prioritized decisions -> trade_intents + outbox     only when ENABLED, in an
                                                                entry session, gate open
    deliver intent events: reserve (6C, risk veto) + submit (7A)   per intent, one txn
    scheduled jobs: risk monitor, reconciliations, worker health, data freshness, session
                    checks, market intelligence, decision outcomes and degradation
                    monitoring after the close (missed days caught up); sweep stuck jobs
    heartbeat with counters and the gate

What happens after an order exists is the existing workers' job: the paper execution
worker delivers it to the isolated paper broker and simulates fills from stored market
data (fills post to the 6A ledger and consume the reservation), and the position monitor
marks positions, enforces their stops and targets and closes them. The orchestrator never
decides a fill, cancellation (except under an operator's emergency stop) or exit itself;
since 10B it runs those workers' own passes while they are down (D-214).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.calendar import session_at
from app.database.models import StrategyProposalRecord
from app.database.models.ai import DecisionRunRecord
from app.database.models.orchestration import OrchestratorStatusRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.events.outbox import OutboxDispatcher
from app.monitoring.workers import WorkerHealth, load_switches, workers_report
from app.orchestration import controls
from app.orchestration.config import OrchestratorSettings
from app.orchestration.enums import JobKind, OrchestratorState
from app.orchestration.intents import INTENT_TOPIC, IntentHandler, take_in
from app.orchestration.jobs import (
    JobOutcome,
    JobRunner,
    JobSpec,
    after_close,
    around_open,
    every,
    previous_days,
)
from app.orchestration.protection import ProtectionGuard
from app.risk.enums import Severity

logger = get_logger(__name__)
STATUS_NAME = "trading-orchestrator"
WORKER_NAME = "orchestrator-worker"  # the Docker Compose service (workers report)
SYSTEM_ACTOR = "system:orchestrator"
UNHEALTHY_REPEAT = timedelta(hours=1)


@dataclass
class Stages:
    """The existing engines the orchestrator drives. Each is optional (None = skipped)."""

    strategy_cycle: Callable[[], Awaitable[dict[str, Any]]] | None = None
    decide: Callable[[], Any] | None = None
    risk_monitor: Callable[[], Any] | None = None
    expire_reservations: Callable[[], Any] | None = None
    reservation_reconcile: Callable[[], Any] | None = None
    execution_reconcile: Callable[[], Any] | None = None
    accounting_reconcile: Callable[[], Any] | None = None
    market_intelligence: Callable[[], Any] | None = None
    decision_outcomes: Callable[[], Any] | None = None
    # Phase 10B.
    degradation_monitor: Callable[[], Any] | None = None
    data_freshness: Callable[[], Any] | None = None
    session_open_check: Callable[[], Any] | None = None
    session_close_review: Callable[[str], Any] | None = None  # takes the day's slot


@dataclass
class TickReport:
    at: datetime
    mode: str
    session: str
    gate_open: bool | None = None
    protection: dict[str, Any] | None = None
    strategy: dict[str, Any] | None = None
    decision_run: str | None = None
    intents_taken: int = 0
    intents_delivered: int = 0
    intents_failed: int = 0
    intents_dead: int = 0
    jobs: list[JobOutcome] = field(default_factory=list)
    swept: int = 0
    errors: list[str] = field(default_factory=list)


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if _jsonable(v)}
    out: dict[str, Any] = {}
    for name in ("status", "result", "run_id", "snapshot_id", "outcome"):
        attr = getattr(value, name, None)
        if attr is not None:
            out[name] = str(getattr(attr, "value", attr))
    return out


def _wrap(fn: Callable[[], Any]) -> Callable[[], dict[str, Any]]:
    return lambda: _summary(fn())


class Orchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        settings: OrchestratorSettings,
        *,
        account_id: uuid.UUID,
        intents: IntentHandler,
        stages: Stages,
        worker_id: str,
        protection: ProtectionGuard | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self.settings = settings
        self.account_id = account_id
        self.intents = intents
        self.stages = stages
        self.worker_id = worker_id
        self.protection = protection
        self.dispatcher = OutboxDispatcher(
            session_factory,
            clock,
            {INTENT_TOPIC: intents},
            max_attempts=settings.intent_max_attempts,
        )
        self.jobs = JobRunner(
            session_factory,
            clock,
            owner=worker_id,
            lease=timedelta(seconds=settings.job_lease_seconds),
            max_attempts=settings.job_max_attempts,
            account_id=account_id,
            catch_up_days=settings.catch_up_days,
        )
        self.counters: dict[str, int] = {}
        self._last_decision_at: datetime | None = None

    # ------------------------------------------------------------------ status
    def _count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def write_status(
        self,
        state: OrchestratorState,
        *,
        mode: str | None = None,
        tick_ms: int | None = None,
        error: str | None = None,
        started: bool = False,
        stopped: bool = False,
        gate: tuple[bool, list[dict[str, Any]]] | None = None,
    ) -> None:
        now = self._clock.now()
        values: dict[str, Any] = {
            "worker_id": self.worker_id,
            "portfolio_account_id": self.account_id,
            "state": state,
            "heartbeat_at": now,
            "counters": dict(self.counters),
        }
        if mode is not None:
            values["trading_mode"] = mode
        if gate is not None:
            values["entry_gate_open"] = gate[0]
            values["entry_gate_reasons"] = gate[1]
        if tick_ms is not None:
            values["last_tick_at"] = now
            values["last_tick_ms"] = tick_ms
        if error is not None:
            values["last_error"] = redact_text(error)[:1000]
            values["last_error_at"] = now
        if started:
            values["started_at"] = now
            values["stopped_at"] = None
        if stopped:
            values["stopped_at"] = now
        with session_scope(self._factory) as session:
            session.execute(
                pg_insert(OrchestratorStatusRecord)
                .values(name=STATUS_NAME, **values)
                .on_conflict_do_update(index_elements=["name"], set_=values)
            )

    def beat(self) -> bool:
        """Renew the heartbeat only (between and during long ticks). False when the status
        row does not exist yet."""
        with session_scope(self._factory) as session:
            row = session.get(OrchestratorStatusRecord, STATUS_NAME, with_for_update=True)
            if row is None or row.worker_id != self.worker_id:
                return False
            row.heartbeat_at = self._clock.now()
            return True

    # ------------------------------------------------------------------ jobs
    def job_specs(self) -> list[JobSpec]:
        s, st = self.settings, self.stages
        specs: list[JobSpec] = []

        def interval(kind: JobKind, seconds: int, fn: Callable[[], Any] | None) -> None:
            if fn is not None:
                specs.append(JobSpec(kind, every(seconds), _wrap(fn), interval_seconds=seconds))

        def daily(kind: JobKind, delay: int, fn: Callable[[], Any] | None, prefix: str) -> None:
            if fn is not None:
                specs.append(
                    JobSpec(
                        kind,
                        after_close(delay, prefix),
                        _wrap(fn),
                        past=previous_days(prefix),
                    )
                )

        interval(JobKind.RISK_MONITOR, s.risk_monitor_seconds, st.risk_monitor)
        interval(
            JobKind.RESERVATION_RECONCILE,
            s.reservation_reconcile_seconds,
            st.reservation_reconcile,
        )
        interval(JobKind.EXECUTION_RECONCILE, s.execution_reconcile_seconds, st.execution_reconcile)
        interval(
            JobKind.ACCOUNTING_RECONCILE,
            s.accounting_reconcile_seconds,
            st.accounting_reconcile,
        )
        interval(JobKind.WORKER_HEALTH, s.worker_health_seconds, self.check_workers)
        interval(JobKind.DATA_FRESHNESS, s.data_freshness_seconds, st.data_freshness)
        if s.run_market_intelligence:
            daily(
                JobKind.MARKET_INTELLIGENCE,
                s.after_close_delay_seconds,
                st.market_intelligence,
                "close",
            )
        if s.run_decision_outcomes:
            daily(
                JobKind.DECISION_OUTCOMES,
                s.after_close_delay_seconds,
                st.decision_outcomes,
                "close",
            )
        if s.run_degradation_monitor:
            daily(
                JobKind.DEGRADATION_MONITOR,
                s.after_close_delay_seconds,
                st.degradation_monitor,
                "close",
            )
        if s.run_session_checks:
            if st.session_open_check is not None:
                specs.append(
                    JobSpec(
                        JobKind.SESSION_OPEN_CHECK,
                        around_open(s.session_open_lead_seconds, s.session_open_window_seconds),
                        _wrap(st.session_open_check),
                    )
                )
            if st.session_close_review is not None:
                review = st.session_close_review
                specs.append(
                    JobSpec(
                        JobKind.SESSION_CLOSE_REVIEW,
                        after_close(s.session_close_review_delay_seconds, "eod"),
                        lambda: _summary(review("")),
                        past=previous_days("eod"),
                        per_slot=True,
                        run_slot=lambda slot: _summary(review(slot)),
                    )
                )
        return specs

    def check_workers(self) -> dict[str, Any]:
        """Report workers whose heartbeat went stale or that recorded a failure. One event
        per worker per ``UNHEALTHY_REPEAT`` (a worker that stays down is not re-reported on
        every check)."""
        bad = {WorkerHealth.STALE, WorkerHealth.FAILED}
        with session_scope(self._factory) as session:
            now = self._clock.now()
            report = workers_report(session, now, load_switches())
            flagged = [w for w in report.workers if w.health in bad and w.worker != WORKER_NAME]
            recent = set(
                session.scalars(
                    select(SystemHealthEventRecord.subject_id).where(
                        SystemHealthEventRecord.component == "orchestration",
                        SystemHealthEventRecord.event_type == "WORKER_UNHEALTHY",
                        SystemHealthEventRecord.occurred_at > now - UNHEALTHY_REPEAT,
                    )
                ).all()
            )
            for w in flagged:
                if w.worker in recent:
                    continue
                session.add(
                    SystemHealthEventRecord(
                        component="orchestration",
                        event_type="WORKER_UNHEALTHY",
                        severity=Severity.HIGH,
                        portfolio_account_id=self.account_id,
                        subject_kind="worker",
                        subject_id=w.worker,
                        summary=f"{w.title}: {w.health.value}. {w.detail}",
                        details={
                            "state": w.state,
                            "heartbeat_age_seconds": w.heartbeat_age_seconds,
                        },
                        occurred_at=now,
                    )
                )
            return {"flagged": [w.worker for w in flagged], "summary": report.summary}

    # ------------------------------------------------------------------ stages
    def _decision_due(self, now: datetime) -> bool:
        """New LIVE strategy proposals since the last LIVE decision run, and not too soon."""
        if self._last_decision_at is not None and now - self._last_decision_at < timedelta(
            seconds=self.settings.min_decision_interval_seconds
        ):
            return False
        with session_scope(self._factory) as session:
            last_run = session.scalar(
                select(func.max(DecisionRunRecord.started_at)).where(
                    DecisionRunRecord.mode == "LIVE"
                )
            )
            newest = session.scalar(
                select(func.max(StrategyProposalRecord.created_at)).where(
                    StrategyProposalRecord.mode == "LIVE"
                )
            )
        if newest is None:
            return False
        return last_run is None or newest > last_run

    async def _step(self, report: TickReport, name: str, work: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(work)
        except Exception as exc:  # recorded; the next tick retries
            report.errors.append(f"{name}: {type(exc).__name__}: {exc}"[:500])
            self._count(f"{name}_errors")
            logger.warning("orchestration.step_failed", step=name, error_type=type(exc).__name__)
            return None

    async def tick(self) -> TickReport:
        started = time.monotonic()
        now = self._clock.now()
        with session_scope(self._factory) as session:
            state = controls.current(session, self.account_id)
        report = TickReport(now, state.mode.value, session_at(now).value)
        st = self.stages

        if st.expire_reservations is not None:
            await self._step(report, "expire_reservations", st.expire_reservations)

        # Phase 10B: protection first. Who manages open positions? Backstop if nobody.
        gate_open = True
        gate_reasons: list[dict[str, Any]] = []
        if self.protection is not None:
            protected = await self._step(report, "protection", self.protection.run)
            if protected is None:
                gate_open = False  # unknown is closed
                gate_reasons = [{"code": "PROTECTION_UNKNOWN", "message": "check failed"}]
            else:
                gate_open = protected.assessment.gate_open
                gate_reasons = protected.assessment.reasons
                report.protection = protected.assessment.as_dict()
                if protected.monitor_backstop is not None:
                    report.protection["monitor_backstop"] = protected.monitor_backstop
                    self._count("monitor_backstop_passes")
                if protected.execution_backstop is not None:
                    report.protection["execution_backstop"] = protected.execution_backstop
                    self._count("execution_backstop_passes")
                report.errors.extend(protected.errors)
            if not gate_open:
                self._count("gate_closed_ticks")
        report.gate_open = gate_open

        if self.settings.run_strategy_stage and st.strategy_cycle is not None:
            try:
                report.strategy = await st.strategy_cycle()
            except Exception as exc:
                report.errors.append(f"strategy: {type(exc).__name__}: {exc}"[:500])
                self._count("strategy_errors")
        if self.settings.run_decision_stage and st.decide is not None:
            due = await self._step(report, "decision_check", lambda: self._decision_due(now))
            if due:
                run = await self._step(report, "decision", st.decide)
                self._last_decision_at = now
                if run is not None:
                    report.decision_run = str(
                        getattr(run, "run_id", None) or getattr(run, "id", "")
                    )
                    self._count("decision_runs")

        entry_session = session_at(now) in self.settings.entry_sessions
        if state.entries_allowed and entry_session and gate_open:

            def intake() -> int:
                with session_scope(self._factory) as session:
                    return len(
                        take_in(
                            session,
                            self.account_id,
                            now=self._clock.now(),
                            lookback=timedelta(minutes=self.settings.intake_lookback_minutes),
                            limit=self.settings.intake_batch,
                        )
                    )

            taken = await self._step(report, "intake", intake)
            report.intents_taken = int(taken or 0)
            self._count("intents_taken", report.intents_taken)

        # Delivered in any mode: a handler that finds trading no longer enabled records
        # NOT_TRADED instead of leaving the intent pending.
        summary = await self._step(
            report, "deliver", lambda: self.dispatcher.dispatch(limit=self.settings.intake_batch)
        )
        if summary is not None:
            report.intents_delivered = summary.delivered
            report.intents_failed = summary.failed
            report.intents_dead = summary.dead
            self._count("intents_delivered", summary.delivered)
            self._count("intents_dead", summary.dead)

        report.swept = int(await self._step(report, "sweep", self.jobs.sweep) or 0)
        jobs = await self._step(report, "jobs", lambda: self.jobs.run_due(self.job_specs()))
        report.jobs = list(jobs or [])
        self._count("ticks")
        elapsed = int((time.monotonic() - started) * 1000)
        self.write_status(
            OrchestratorState.DEGRADED if report.errors else OrchestratorState.RUNNING,
            mode=state.mode.value,
            tick_ms=elapsed,
            error="; ".join(report.errors) if report.errors else None,
            gate=(gate_open, gate_reasons),
        )
        logger.info(
            "orchestration.tick",
            mode=report.mode,
            session=report.session,
            gate_open=gate_open,
            taken=report.intents_taken,
            delivered=report.intents_delivered,
            dead=report.intents_dead,
            jobs=[f"{j.kind.value}:{j.status.value}" for j in report.jobs],
            errors=len(report.errors),
            ms=elapsed,
        )
        return report


__all__ = [
    "STATUS_NAME",
    "SYSTEM_ACTOR",
    "UNHEALTHY_REPEAT",
    "WORKER_NAME",
    "Orchestrator",
    "Stages",
    "TickReport",
]

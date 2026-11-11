"""Wiring of the validation laboratory for the API and the CLI (Phase 8B).

Validation runs submitted through the API execute on one background thread per API
process, one at a time and in submission order (a run replays many backtests). Holdout
evaluations, governance decisions and monitoring passes run in the request.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.service import BacktestService
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models.learning import ValidationRunRecord
from app.database.sessions import session_scope
from app.learning.enums import RunStatus
from app.learning.governance import GovernanceService, Policy
from app.learning.monitoring import MonitoringService
from app.learning.service import ValidationService
from app.strategies.registry import StrategyRegistry, default_registry

logger = get_logger(__name__)


@dataclass
class ValidationContext:
    session_factory: sessionmaker[Session]
    clock: Clock
    registry: StrategyRegistry
    service: ValidationService
    governance: GovernanceService
    monitoring: MonitoringService
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def submit(self, run_id: uuid.UUID) -> Future[RunStatus]:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="validation")
            return self._executor.submit(self._execute, run_id)

    def _execute(self, run_id: uuid.UUID) -> RunStatus:
        try:
            return self.service.execute(run_id)
        except Exception:  # the service records failures; this only logs the unexpected
            logger.exception("validation.execute_crashed", run_id=str(run_id))
            return RunStatus.FAILED

    def resume(self) -> int:
        self.service.fail_stale()
        with session_scope(self.session_factory) as session:
            queued = session.scalars(
                select(ValidationRunRecord.id)
                .where(ValidationRunRecord.status == RunStatus.QUEUED)
                .order_by(ValidationRunRecord.created_at)
            ).all()
        for run_id in queued:
            self.submit(run_id)
        return len(queued)

    def drain(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def shutdown(self) -> None:
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None


def build_validation_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    registry: StrategyRegistry | None = None,
    backtests: BacktestService | None = None,
    policy: Policy | None = None,
) -> ValidationContext:
    registry = registry or default_registry()
    backtests = backtests or BacktestService(session_factory, clock, registry=registry)
    return ValidationContext(
        session_factory=session_factory,
        clock=clock,
        registry=registry,
        service=ValidationService(session_factory, clock, backtests),
        governance=GovernanceService(session_factory, clock, registry, backtests, policy),
        monitoring=MonitoringService(session_factory, clock, registry),
    )


__all__ = ["ValidationContext", "build_validation_context"]

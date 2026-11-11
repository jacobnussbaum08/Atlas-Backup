"""Wiring of the backtester for the API and the CLI (Phase 8A).

Runs submitted through the API execute on one background thread per API process, one at a
time, so a long replay never competes with another for the database or the CPU and the
order of execution is the order of submission.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.enums import RunStatus
from app.backtesting.service import BacktestService
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models import BacktestRunRecord
from app.database.sessions import session_scope
from app.strategies.registry import StrategyRegistry, default_registry

logger = get_logger(__name__)


@dataclass
class BacktestContext:
    session_factory: sessionmaker[Session]
    clock: Clock
    registry: StrategyRegistry
    service: BacktestService
    provider: str = "alpaca"
    feed: str = "iex"
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def submit(self, run_id: uuid.UUID) -> Future[RunStatus]:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
            return self._executor.submit(self._execute, run_id)

    def _execute(self, run_id: uuid.UUID) -> RunStatus:
        try:
            return self.service.execute(run_id)
        except Exception:  # the service records failures; this only logs the unexpected
            logger.exception("backtest.execute_crashed", run_id=str(run_id))
            return RunStatus.FAILED

    def resume(self) -> int:
        """Fail runs whose process ended mid-replay; queue runs that never started."""
        self.service.fail_stale()
        with session_scope(self.session_factory) as session:
            queued = session.scalars(
                select(BacktestRunRecord.id)
                .where(BacktestRunRecord.status == RunStatus.QUEUED)
                .order_by(BacktestRunRecord.created_at)
            ).all()
        for run_id in queued:
            self.submit(run_id)
        return len(queued)

    def drain(self) -> None:
        """Wait for every submitted run to finish (tests, the CLI)."""
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def shutdown(self) -> None:
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None


def build_backtest_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    provider: str = "alpaca",
    feed: str = "iex",
    registry: StrategyRegistry | None = None,
) -> BacktestContext:
    registry = registry or default_registry()
    service = BacktestService(session_factory, clock, registry=registry)
    return BacktestContext(session_factory, clock, registry, service, provider, feed)


__all__ = ["BacktestContext", "build_backtest_context"]

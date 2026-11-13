"""Shared setup for the Phase 10A orchestration integration tests.

TEST FIXTURE material only (see ``execution_support``): CONTROLLED_TEST accounts funded with
simulated capital, invented instruments with MOCK bars, fixture strategy proposals and
fixture AI decisions written straight into the database. Every order is a **simulated paper
order**; nothing reaches a brokerage. The research stages (strategy cycle, decision run,
market intelligence, decision outcomes) are replaced by counting stubs, so no provider is
contacted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.accounting.config import AccountingSettings
from app.core.clocks import FixedClock
from app.database.models import AIDecisionRecord
from app.database.models.ai import (
    DecisionPolicyRecord,
    DecisionPolicyVersionRecord,
    DecisionRunRecord,
)
from app.database.models.execution import OrderRecord
from app.database.models.orchestration import TradeIntentRecord
from app.database.models.reservations import RiskReservationRecord, SystemHealthEventRecord
from app.database.sessions import session_scope
from app.orchestration import controls
from app.orchestration.config import OrchestratorSettings
from app.orchestration.enums import ControlAction, TradingMode
from app.orchestration.runtime import (
    ControlContext,
    OrchestratorRuntime,
    build_control_context,
    build_orchestrator,
)
from app.strategies.enums import StrategyFamily
from app.tests.integration.ai_support import make_proposal, make_strategy
from app.tests.integration.execution_support import Execution, make_execution

OPERATOR = "operator:test-desk"
ACCOUNTING = AccountingSettings(_env_file=None)


def settings(**overrides: Any) -> OrchestratorSettings:
    values: dict[str, Any] = {
        "worker_enabled": True,
        "run_strategy_stage": False,
        "run_decision_stage": False,
        "run_market_intelligence": False,
        "run_decision_outcomes": False,
        # Phase 10B: the 10A tests run without worker processes; the 10B tests switch the
        # protection gate on and write the workers' heartbeats themselves.
        "require_protection_workers": False,
        "run_degradation_monitor": False,
    }
    values.update(overrides)
    return OrchestratorSettings(_env_file=None, **values)


@dataclass
class Calls:
    counts: dict[str, int] = field(default_factory=dict)

    def stub(self, name: str, *, fail: int = 0) -> Any:
        """A sync stage that records its calls and raises on its first ``fail`` calls."""

        def body() -> dict[str, Any]:
            self.counts[name] = self.counts.get(name, 0) + 1
            if self.counts[name] <= fail:
                raise RuntimeError(f"{name} failed (TEST FIXTURE)")
            return {"fixture": True}

        return body


@dataclass
class Bench:
    ex: Execution
    account: uuid.UUID
    account_name: str
    control: ControlContext
    calls: Calls

    @property
    def factory(self) -> Any:
        return self.ex.factory

    @property
    def clock(self) -> FixedClock:
        return self.ex.rv.risk.clock

    def now(self) -> datetime:
        return self.clock.now()

    # ---------------------------------------------------------------- orchestrator
    def orchestrator(
        self,
        *,
        worker_id: str = "orchestrator:test-1",
        research: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> OrchestratorRuntime:
        return build_orchestrator(
            self.factory,
            self.clock,
            settings(account_name=self.account_name, **overrides),
            worker_id=worker_id,
            accounting=ACCOUNTING,
            research={} if research is None else research,
        )

    # ---------------------------------------------------------------- controls
    def set_mode(self, action: ControlAction, reason: str = "test control change") -> None:
        self.control.controls.change(self.account, action, actor=OPERATOR, reason=reason)

    def enable(self) -> None:
        self.set_mode(ControlAction.ENABLE)

    def mode(self) -> TradingMode:
        with session_scope(self.factory) as session:
            return controls.current(session, self.account).mode

    # ---------------------------------------------------------------- fixtures
    def asset(self, symbol: str) -> uuid.UUID:
        return self.ex.asset(symbol)

    def decision(
        self,
        asset: uuid.UUID,
        *,
        symbol: str = "ORCA",
        outcome: str = "PRIORITIZE_FOR_REVIEW",
        entry: str = "99.70",
        age: timedelta = timedelta(minutes=10),
        lifetime: timedelta = timedelta(hours=6),
        mode: str = "LIVE",
        run_status: str = "COMPLETED",
    ) -> uuid.UUID:
        """A fixture LIVE coordinator run with one decision whose lead proposal is a fixture
        strategy proposal (TEST FIXTURE; scores are never read by the orchestrator)."""
        now = self.now()
        strategy_id = f"momentum_fixture_{uuid.uuid4().hex[:6]}"
        version = make_strategy(self.factory, strategy_id, StrategyFamily.MOMENTUM)
        lead = make_proposal(
            self.factory,
            strategy_id=strategy_id,
            version_id=version,
            asset_id=asset,
            stored_at=now - age - timedelta(minutes=5),
            cutoff=now - age - timedelta(hours=1),
            entry=entry,
            expires_at=now + timedelta(days=1),
        )
        with session_scope(self.factory) as session:
            policy = DecisionPolicyRecord(
                code=f"fixture_{uuid.uuid4().hex[:6]}", description="TEST FIXTURE policy"
            )
            session.add(policy)
            session.flush()
            version_row = DecisionPolicyVersionRecord(
                policy_id=policy.id,
                version=1,
                parameters={},
                parameters_sha256=uuid.uuid4().hex * 2,
                logic_version="fixture",
                reason="fixture",
                created_by="test",
            )
            session.add(version_row)
            session.flush()
            at = now - age
            run = DecisionRunRecord(
                policy_version_id=version_row.id,
                mode=mode,
                basis="RECORDED",
                trigger="test",
                decision_at=at,
                knowledge_at=at,
                status=run_status,
                error="TEST FIXTURE failure" if run_status == "FAILED" else None,
                outcome=None if run_status == "FAILED" else "OPPORTUNITIES_PRIORITIZED",
                code_version={},
                worker_id="test",
                created_by="test",
                started_at=at,
                finished_at=at,
            )
            session.add(run)
            session.flush()
            decision = AIDecisionRecord(
                run_id=run.id,
                outcome=outcome,
                rank=1 if outcome == "PRIORITIZE_FOR_REVIEW" else None,
                asset_id=asset,
                symbol=symbol,
                direction="LONG",
                proposal_ids=[lead],
                policy_version_id=version_row.id,
                scores={"heuristic_score": 0.5},
                expected={"lead_proposal_id": str(lead)},
                uncertainty={},
                explanation="TEST FIXTURE decision",
                decision_at=at,
                data_cutoff=at,
                expires_at=at + lifetime,
                content_sha256="0" * 64,
            )
            session.add(decision)
            session.flush()
            return decision.id

    # ---------------------------------------------------------------- reads
    def count(self, model: Any, *where: Any) -> int:
        with session_scope(self.factory) as session:
            return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    def intents(self) -> list[TradeIntentRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(TradeIntentRecord)
                .where(TradeIntentRecord.portfolio_account_id == self.account)
                .order_by(TradeIntentRecord.received_at, TradeIntentRecord.id)
            ).all()
            session.expunge_all()
            return list(rows)

    def orders(self) -> list[OrderRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(OrderRecord)
                .where(OrderRecord.portfolio_account_id == self.account)
                .order_by(OrderRecord.submitted_at, OrderRecord.id)
            ).all()
            session.expunge_all()
            return list(rows)

    def reservations(self) -> list[RiskReservationRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(RiskReservationRecord).where(
                    RiskReservationRecord.portfolio_account_id == self.account
                )
            ).all()
            session.expunge_all()
            return list(rows)

    def health_events(self, event_type: str | None = None) -> list[SystemHealthEventRecord]:
        with session_scope(self.factory) as session:
            q = select(SystemHealthEventRecord).where(
                SystemHealthEventRecord.component == "orchestration"
            )
            if event_type is not None:
                q = q.where(SystemHealthEventRecord.event_type == event_type)
            rows = session.scalars(q.order_by(SystemHealthEventRecord.id)).all()
            session.expunge_all()
            return list(rows)


def make_bench(engine: Any, name: str = "orch-test", *, now: datetime | None = None) -> Bench:
    """``now`` moves the fixed clock (default: the fixtures' NOW, 2026-11-03 22:30 UTC)."""
    ex = make_execution(engine) if now is None else make_execution(engine, now=now)
    account = ex.account(name)
    control = build_control_context(ex.factory, ex.risk.clock, accounting=ACCOUNTING)
    return Bench(ex, account, name, control, Calls())


__all__ = ["ACCOUNTING", "OPERATOR", "Bench", "Calls", "make_bench", "settings"]

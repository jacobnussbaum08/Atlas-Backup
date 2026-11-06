"""The risk service: assessments, monitoring and circuit-breaker control (Phase 6B).

Every operation is one database transaction that first locks the portfolio account row
(``SELECT ... FOR UPDATE``, the lock 6A postings already take), so an assessment sees one
consistent ledger, breaker state and configuration, and breaker transitions never race.

``assess`` records every decision with every check and the exact inputs, so it can be
replayed (``replay``). A PASS_FOR_RESERVATION is **not** an execution approval and reserves
nothing by itself; active reservations (Phase 6C) are part of the assessed state. Only
``app.risk.reservations.service`` reserves capacity: it runs this same assessment inside
its own transaction, under the account lock, and persists the reservation atomically.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.core.clocks import Clock
from app.core.exceptions import AtlasError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models import Asset, PortfolioAccount
from app.database.models.risk import (
    RiskAssessmentReasonRecord,
    RiskAssessmentRecord,
    RiskEventRecord,
)
from app.database.sessions import session_scope
from app.risk import breakers as rules
from app.risk import data, governance
from app.risk.assessment import ENGINE_VERSION, assess
from app.risk.config import Preferences, RiskConfiguration, canonical_json
from app.risk.enums import (
    ActorKind,
    AssessmentResult,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    RiskEventType,
    Severity,
    SnapshotTrigger,
)
from app.risk.models import AssessmentInputs, AssessmentOutcome, Proposal
from app.risk.state import (
    EvaluatedState,
    breaker_statuses,
    evaluate,
    lock_account,
    rate_sources,
    record_breaker_event,
)

logger = get_logger(__name__)
DEFAULT_TTL = timedelta(seconds=300)


class RiskControlError(AtlasError):
    """A circuit-breaker action was refused."""

    code = "risk_control_refused"
    http_status = 409


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AssessmentRecordResult:
    assessment_id: uuid.UUID
    result: AssessmentResult
    permitted_quantity: Decimal | None
    created: bool
    outcome: AssessmentOutcome | None


class RiskService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        accounting: AccountingSettings,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._accounting = accounting

    # ------------------------------------------------------------------ assessments
    def assess(
        self,
        account_id: uuid.UUID,
        proposal: Proposal,
        *,
        requested_by: str,
        request_key: str | None = None,
    ) -> AssessmentRecordResult:
        with session_scope(self._factory) as session:
            return self.assess_in(
                session, account_id, proposal, requested_by=requested_by, request_key=request_key
            )

    def assess_in(
        self,
        session: Session,
        account_id: uuid.UUID,
        proposal: Proposal,
        *,
        requested_by: str,
        request_key: str | None = None,
    ) -> AssessmentRecordResult:
        if not requested_by.strip():
            raise InvalidRequestError("requested_by is required")
        account = lock_account(session, account_id)
        if request_key is not None:
            existing = session.scalar(
                select(RiskAssessmentRecord).where(
                    RiskAssessmentRecord.portfolio_account_id == account_id,
                    RiskAssessmentRecord.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.inputs.get("proposal") != proposal.model_dump(mode="json"):
                    raise InvalidRequestError(
                        f"request key {request_key!r} was used for a different proposal"
                    )
                return AssessmentRecordResult(
                    existing.id, existing.result, existing.permitted_quantity, False, None
                )
        asset = session.get(Asset, proposal.asset_id)
        if asset is None:
            raise NotFoundError(f"asset {proposal.asset_id} does not exist")
        at = self._clock.now()
        config = governance.active_configuration(session, account_id)
        evaluated = evaluate(
            session,
            account,
            at,
            config,
            self._accounting,
            persist=True,
            trigger=SnapshotTrigger.ASSESSMENT,
        )
        inputs = self._inputs(session, account, asset, proposal, evaluated, config, at)
        outcome = assess(inputs)
        record = self._store(
            session, account_id, asset, inputs, outcome, evaluated, requested_by, request_key
        )
        return AssessmentRecordResult(
            record.id, outcome.result, outcome.permitted_quantity, True, outcome
        )

    def _inputs(
        self,
        session: Session,
        account: PortfolioAccount,
        asset: Asset,
        proposal: Proposal,
        evaluated: EvaluatedState,
        config: RiskConfiguration | None,
        at: datetime,
    ) -> AssessmentInputs:
        preferences = config.preferences if config is not None else Preferences()
        market = data.market_data(
            session,
            asset,
            at,
            functional_currency=account.base_currency,
            accounting=self._accounting,
            preferences=preferences,
            rate_sources=rate_sources(account),
        )
        # Held and reserved assets: both count toward the correlated-cluster limit.
        held = list(
            dict.fromkeys(
                [p.asset_id for p in evaluated.view.positions]
                + [r.asset_id for r in evaluated.view.reservations]
            )
        )
        correlations = data.correlations(
            session,
            asset.id,
            held,
            at,
            timedelta(days=preferences.correlation_max_age_days),
        )
        return AssessmentInputs(
            assessed_at=at,
            proposal=proposal,
            instrument=data.instrument_data(session, asset, at),
            market=market,
            portfolio=evaluated.view,
            correlations=correlations,
            config=config,
        )

    def _store(
        self,
        session: Session,
        account_id: uuid.UUID,
        asset: Asset,
        inputs: AssessmentInputs,
        outcome: AssessmentOutcome,
        evaluated: EvaluatedState,
        requested_by: str,
        request_key: str | None,
    ) -> RiskAssessmentRecord:
        config = inputs.config
        inputs_json = inputs.model_dump(mode="json")
        outcome_json = outcome.model_dump(mode="json")
        ttl = (
            timedelta(seconds=config.preferences.assessment_ttl_seconds)
            if config is not None
            else DEFAULT_TTL
        )
        p = inputs.proposal
        record = RiskAssessmentRecord(
            portfolio_account_id=account_id,
            request_key=request_key,
            source=p.source,
            strategy_proposal_id=p.strategy_proposal_id,
            ai_decision_id=p.ai_decision_id,
            asset_id=asset.id,
            symbol=asset.symbol,
            asset_class=asset.asset_class.value,
            direction=p.direction,
            entry=p.entry,
            stop=p.stop,
            target=p.target,
            requested_quantity=p.requested_quantity,
            result=outcome.result,
            permitted_quantity=outcome.permitted_quantity,
            functional_currency=inputs.portfolio.functional_currency,
            equity=inputs.portfolio.equity,
            risk_budget=outcome.risk_budget,
            potential_loss=outcome.potential_loss,
            notional=outcome.notional,
            estimated_costs=outcome.estimated_costs,
            binding_constraint=outcome.binding_constraint,
            inputs=inputs_json,
            inputs_sha256=_sha(inputs_json),
            calculations=outcome.calculations,
            exposure_after=outcome.exposure_after,
            exposure_snapshot_id=evaluated.snapshot_id,
            config_version_id=None
            if config is None or config.version_id is None
            else uuid.UUID(config.version_id),
            ledger_sequence=inputs.portfolio.ledger_sequence,
            assessed_at=inputs.assessed_at,
            valid_until=inputs.assessed_at + ttl,
            engine_version=ENGINE_VERSION,
            requested_by=requested_by,
            content_sha256=_sha({"inputs": inputs_json, "outcome": outcome_json}),
        )
        session.add(record)
        session.flush()
        for ordinal, check in enumerate(outcome.checks, start=1):
            session.add(
                RiskAssessmentReasonRecord(
                    assessment_id=record.id,
                    ordinal=ordinal,
                    check_code=check.code,
                    outcome=check.outcome,
                    reason_code=None if check.reason_code is None else check.reason_code.value,
                    message=check.message,
                    measured=check.measured,
                    limit_value=check.limit,
                    unit=check.unit,
                    details=json.loads(canonical_json(check.details)),
                )
            )
        reasons = [c.reason_code.value for c in outcome.reasons if c.reason_code is not None]
        session.add(
            RiskEventRecord(
                portfolio_account_id=account_id,
                event_type=RiskEventType.ASSESSMENT_RECORDED,
                severity=Severity.INFO,
                occurred_at=inputs.assessed_at,
                actor=requested_by,
                subject_kind="risk_assessment",
                subject_id=str(record.id),
                summary=f"{asset.symbol} {p.direction.value}: {outcome.result.value}",
                details={"reasons": reasons, "permitted_quantity": str(outcome.permitted_quantity)},
            )
        )
        session.flush()
        logger.info(
            "risk.assessment",
            account_id=str(account_id),
            assessment_id=str(record.id),
            symbol=asset.symbol,
            direction=p.direction.value,
            result=outcome.result.value,
            reasons=reasons,
        )
        return record

    # ------------------------------------------------------------------ monitoring
    def monitor(
        self, account_id: uuid.UUID, *, actor: str = "system:risk-monitor"
    ) -> EvaluatedState:
        """Evaluate and record the account's risk state; trip (or auto-recover) breakers."""
        with session_scope(self._factory) as session:
            account = lock_account(session, account_id)
            config = governance.active_configuration(session, account_id)
            at = self._clock.now()
            state = evaluate(
                session,
                account,
                at,
                config,
                self._accounting,
                persist=True,
                trigger=SnapshotTrigger.MONITOR,
                actor=actor,
            )
            session.add(
                RiskEventRecord(
                    portfolio_account_id=account_id,
                    event_type=RiskEventType.STATE_EVALUATED,
                    severity=Severity.INFO,
                    occurred_at=at,
                    actor=actor,
                    subject_kind="risk_exposure_snapshot",
                    subject_id=str(state.snapshot_id),
                    summary=f"risk state {state.view.mark_status.value}; "
                    f"{len(state.transitions)} breaker transition(s)",
                    details={
                        "transitions": [
                            f"{t.breaker.value} {t.event.value}" for t in state.transitions
                        ]
                    },
                )
            )
            return state

    def preview(self, account_id: uuid.UUID) -> tuple[EvaluatedState, RiskConfiguration | None]:
        """The live state without writing anything (for the read-only API)."""
        with session_scope(self._factory) as session:
            account = session.get(PortfolioAccount, account_id)
            if account is None:
                raise NotFoundError(f"portfolio account {account_id} does not exist")
            config = governance.active_configuration(session, account_id)
            state = evaluate(
                session, account, self._clock.now(), config, self._accounting, persist=False
            )
            session.expunge_all()
            return state, config

    # ------------------------------------------------------------------ breaker control
    def engage_kill_switch(self, account_id: uuid.UUID, *, actor: str, reason: str) -> bool:
        """Engage the manual kill switch. Returns False if it was already engaged."""
        governance.require_operator(actor)
        with session_scope(self._factory) as session:
            account = lock_account(session, account_id)
            statuses = _statuses(session, account.id)
            if statuses[BreakerType.KILL_SWITCH] is BreakerStatus.TRIPPED:
                return False
            config = governance.active_configuration(session, account_id)
            record_breaker_event(
                session,
                account_id,
                BreakerType.KILL_SWITCH,
                BreakerEvent.TRIPPED,
                at=self._clock.now(),
                actor=actor,
                actor_kind=ActorKind.OPERATOR,
                reason=_reason(reason),
                measurements={"manual": True},
                config_version_id=None if config is None else config.version_id,
            )
            return True

    def reset_breaker(
        self, account_id: uuid.UUID, breaker: BreakerType, *, actor: str, reason: str
    ) -> None:
        """Operator reset (kill-switch release). Refused while the condition is breached or
        cannot be evaluated; the refusal is recorded."""
        governance.require_operator(actor)
        reason = _reason(reason)
        refused: str | None = None
        with session_scope(self._factory) as session:
            account = lock_account(session, account_id)
            statuses = _statuses(session, account.id)
            if statuses[breaker] is not BreakerStatus.TRIPPED:
                raise RiskControlError(f"{breaker.value} is not tripped")
            config = governance.active_configuration(session, account_id)
            at = self._clock.now()
            state = evaluate(
                session,
                account,
                at,
                config,
                self._accounting,
                persist=True,
                trigger=SnapshotTrigger.BREAKER_RESET,
                actor=actor,
            )
            condition = next((c for c in state.conditions if c.breaker is breaker), None)
            allowed, why = rules.reset_allowed(breaker, condition)
            if allowed and state.statuses[breaker] is BreakerStatus.TRIPPED:
                record_breaker_event(
                    session,
                    account_id,
                    breaker,
                    BreakerEvent.RESET,
                    at=at,
                    actor=actor,
                    actor_kind=ActorKind.OPERATOR,
                    reason=f"{reason} ({why})",
                    measurements={} if condition is None else condition.measurements(),
                    config_version_id=None if config is None else config.version_id,
                    snapshot_id=state.snapshot_id,
                )
            elif not allowed:
                refused = why
                session.add(
                    RiskEventRecord(
                        portfolio_account_id=account_id,
                        event_type=RiskEventType.BREAKER_RESET_REFUSED,
                        severity=Severity.WARNING,
                        occurred_at=at,
                        actor=actor,
                        subject_kind="risk_circuit_breaker",
                        subject_id=breaker.value,
                        summary=f"reset of {breaker.value} refused: {why}",
                        details={"reason_given": reason},
                    )
                )
        if refused is not None:
            raise RiskControlError(f"{breaker.value} not reset: {refused}")

    def report_execution_safety(
        self, account_id: uuid.UUID, *, source: str, reason: str, details: dict[str, Any]
    ) -> bool:
        """Execution-safety interface for the execution phases: trips EXECUTION_SAFETY.

        Phase 6B has no execution and therefore no producer; Phase 7 reports simulated
        execution errors here. Returns False if the breaker was already tripped."""
        if not source.startswith(("system:", "operator:")):
            raise InvalidRequestError("source must be a system: or operator: identity")
        with session_scope(self._factory) as session:
            account = lock_account(session, account_id)
            if _statuses(session, account.id)[BreakerType.EXECUTION_SAFETY] is (
                BreakerStatus.TRIPPED
            ):
                return False
            config = governance.active_configuration(session, account_id)
            record_breaker_event(
                session,
                account_id,
                BreakerType.EXECUTION_SAFETY,
                BreakerEvent.TRIPPED,
                at=self._clock.now(),
                actor=source,
                actor_kind=ActorKind.OPERATOR
                if source.startswith("operator:")
                else ActorKind.SYSTEM,
                reason=_reason(reason),
                measurements=details,
                config_version_id=None if config is None else config.version_id,
            )
            return True

    # ------------------------------------------------------------------ replay
    def replay(self, assessment_id: uuid.UUID) -> tuple[bool, AssessmentOutcome]:
        """Re-run a stored assessment from its stored inputs; True when it reproduces."""
        with session_scope(self._factory) as session:
            record = session.get(RiskAssessmentRecord, assessment_id)
            if record is None:
                raise NotFoundError(f"risk assessment {assessment_id} does not exist")
            inputs = AssessmentInputs.model_validate(record.inputs)
            outcome = assess(inputs)
            same = (
                _sha({"inputs": record.inputs, "outcome": outcome.model_dump(mode="json")})
                == record.content_sha256
            )
            return same, outcome


def _statuses(session: Session, account_id: uuid.UUID) -> dict[BreakerType, BreakerStatus]:
    return breaker_statuses(session, account_id)


def _reason(reason: str) -> str:
    if len((reason or "").strip()) < 3:
        raise InvalidRequestError("a reason is required")
    return reason.strip()


__all__ = ["AssessmentRecordResult", "RiskControlError", "RiskService"]

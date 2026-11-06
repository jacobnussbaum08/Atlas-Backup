"""Read models for the Risk Center API and CLI (Phase 6B). Read-only.

Decimals are serialised as strings. "Live" figures come from an evaluation that writes
nothing (``RiskService.preview``); recorded state (breakers, snapshots, assessments,
configuration history) comes from the append-only risk tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.database.models import PortfolioAccount
from app.database.models.risk import (
    RiskAssessmentReasonRecord,
    RiskAssessmentRecord,
    RiskCircuitBreakerEventRecord,
    RiskConfigurationVersionRecord,
    RiskEventRecord,
    RiskExposureSnapshotRecord,
)
from app.risk import breakers as rules
from app.risk.assessment import NOT_AN_APPROVAL
from app.risk.config import DEFAULTS_NOTE
from app.risk.enums import AssessmentResult, BreakerStatus, BreakerType
from app.risk.governance import latest_version
from app.risk.periods import TIMEZONE_POLICY
from app.risk.state import EvaluatedState, breakdown, breaker_statuses

PAPER_NOTE = (
    "Simulated paper account. The risk engine assesses proposals; it executes nothing, "
    "reserves nothing (Phase 6C) and never liquidates positions."
)
NOT_CONFIGURED = "no risk configuration exists for this account: run `python -m app.risk.cli init`"


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _t(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _version(row: RiskConfigurationVersionRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "version": row.version,
        "change_kind": row.change_kind.value,
        "previous_version_id": None
        if row.previous_version_id is None
        else str(row.previous_version_id),
        "hard_limits": row.hard_limits,
        "preferences": row.preferences,
        "hard_limits_sha256": row.hard_limits_sha256,
        "preferences_sha256": row.preferences_sha256,
        "changes": row.changes,
        "actor": row.actor,
        "reason": row.reason,
        "created_at": _t(row.created_at),
    }


def limits(session: Session, account: PortfolioAccount) -> dict[str, Any]:
    row = latest_version(session, account.id)
    return {
        "account_id": str(account.id),
        "configured": row is not None,
        "message": None if row is not None else NOT_CONFIGURED,
        "active": None if row is None else _version(row),
        "note": DEFAULTS_NOTE,
        "governance": (
            "Hard limits change only through a new immutable version written by an operator "
            "(operator:<name>) with a reason, from the CLI. No strategy, model, LLM or API "
            "call can change them."
        ),
        "breaker_recovery": {b.value: rules.RECOVERY[b].value for b in BreakerType},
        "breaker_response": rules.RESPONSE,
        "timezone_policy": TIMEZONE_POLICY,
    }


def configurations(session: Session, account: PortfolioAccount, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RiskConfigurationVersionRecord)
        .where(RiskConfigurationVersionRecord.portfolio_account_id == account.id)
        .order_by(RiskConfigurationVersionRecord.version.desc())
        .limit(limit)
    )
    return [_version(r) for r in rows]


def breakers(session: Session, account: PortfolioAccount) -> list[dict[str, Any]]:
    statuses = breaker_statuses(session, account.id)
    e = RiskCircuitBreakerEventRecord
    latest = {
        row.breaker_type: row
        for row in session.scalars(
            select(e)
            .where(e.portfolio_account_id == account.id)
            .ext(distinct_on(e.breaker_type))
            .order_by(e.breaker_type, e.sequence.desc())
        )
    }
    out = []
    for breaker in BreakerType:
        row = latest.get(breaker)
        out.append(
            {
                "breaker": breaker.value,
                "status": statuses[breaker].value,
                "recovery": rules.RECOVERY[breaker].value,
                "since": None if row is None else _t(row.occurred_at),
                "last_event": None if row is None else row.event.value,
                "actor": None if row is None else row.actor,
                "reason": None if row is None else row.reason,
                "events": 0 if row is None else row.sequence,
            }
        )
    return out


def _event(row: RiskCircuitBreakerEventRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "breaker": row.breaker_type.value,
        "sequence": row.sequence,
        "event": row.event.value,
        "previous_status": row.previous_status.value,
        "new_status": row.new_status.value,
        "occurred_at": _t(row.occurred_at),
        "actor_kind": row.actor_kind.value,
        "actor": row.actor,
        "reason": row.reason,
        "measurements": row.measurements,
        "response": row.response,
        "config_version_id": None if row.config_version_id is None else str(row.config_version_id),
        "exposure_snapshot_id": None
        if row.exposure_snapshot_id is None
        else str(row.exposure_snapshot_id),
    }


def breaker_events(
    session: Session, account: PortfolioAccount, limit: int, breaker: BreakerType | None = None
) -> list[dict[str, Any]]:
    e = RiskCircuitBreakerEventRecord
    stmt = select(e).where(e.portfolio_account_id == account.id)
    if breaker is not None:
        stmt = stmt.where(e.breaker_type == breaker)
    rows = session.scalars(stmt.order_by(e.occurred_at.desc(), e.id.desc()).limit(limit))
    return [_event(r) for r in rows]


def _snapshot(row: RiskExposureSnapshotRecord, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(row.id),
        "as_of": _t(row.as_of),
        "trigger": row.trigger.value,
        "mark_status": row.mark_status.value,
        "ledger_sequence": row.ledger_sequence,
        "functional_currency": row.functional_currency,
        "equity": _s(row.equity),
        "contributed_capital": _s(row.contributed_capital),
        "available_capital": _s(row.available_capital),
        "long_exposure": _s(row.long_exposure),
        "short_exposure": _s(row.short_exposure),
        "gross_exposure": _s(row.gross_exposure),
        "net_exposure": _s(row.net_exposure),
        "margin_used": _s(row.margin_used),
        "pending_exposure": _s(row.pending_exposure),
        "open_positions": row.open_positions,
        "daily_loss": _s(row.daily_loss),
        "weekly_loss": _s(row.weekly_loss),
        "drawdown": _s(row.drawdown),
        "peak_equity": _s(row.peak_equity),
        "available_risk_budget": _s(row.available_risk_budget),
    }
    if full:
        out |= {
            "breakdown": row.breakdown,
            "issues": row.issues,
            "conditions": row.conditions,
            "content_sha256": row.content_sha256,
        }
    return out


def snapshots(session: Session, account: PortfolioAccount, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RiskExposureSnapshotRecord)
        .where(RiskExposureSnapshotRecord.portfolio_account_id == account.id)
        .order_by(RiskExposureSnapshotRecord.as_of.desc(), RiskExposureSnapshotRecord.id)
        .limit(limit)
    )
    return [_snapshot(r) for r in rows]


def latest_snapshot(session: Session, account: PortfolioAccount) -> dict[str, Any] | None:
    row = session.scalar(
        select(RiskExposureSnapshotRecord)
        .where(RiskExposureSnapshotRecord.portfolio_account_id == account.id)
        .order_by(RiskExposureSnapshotRecord.as_of.desc())
        .limit(1)
    )
    return None if row is None else _snapshot(row, full=True)


def _assessment(row: RiskAssessmentRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "assessed_at": _t(row.assessed_at),
        "valid_until": _t(row.valid_until),
        "result": row.result.value,
        "source": row.source.value,
        "strategy_proposal_id": None
        if row.strategy_proposal_id is None
        else str(row.strategy_proposal_id),
        "ai_decision_id": None if row.ai_decision_id is None else str(row.ai_decision_id),
        "asset_id": str(row.asset_id),
        "symbol": row.symbol,
        "asset_class": row.asset_class,
        "direction": row.direction.value,
        "entry": _s(row.entry),
        "stop": _s(row.stop),
        "target": _s(row.target),
        "requested_quantity": _s(row.requested_quantity),
        "permitted_quantity": _s(row.permitted_quantity),
        "functional_currency": row.functional_currency,
        "equity": _s(row.equity),
        "risk_budget": _s(row.risk_budget),
        "potential_loss": _s(row.potential_loss),
        "notional": _s(row.notional),
        "estimated_costs": _s(row.estimated_costs),
        "binding_constraint": row.binding_constraint,
        "config_version_id": None if row.config_version_id is None else str(row.config_version_id),
        "ledger_sequence": row.ledger_sequence,
        "requested_by": row.requested_by,
        "engine_version": row.engine_version,
    }


def assessments(
    session: Session,
    account: PortfolioAccount,
    limit: int,
    result: AssessmentResult | None = None,
) -> list[dict[str, Any]]:
    a = RiskAssessmentRecord
    stmt = select(a).where(a.portfolio_account_id == account.id)
    if result is not None:
        stmt = stmt.where(a.result == result)
    rows = session.scalars(stmt.order_by(a.assessed_at.desc(), a.id).limit(limit)).all()
    ids = [r.id for r in rows]
    decisive: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    if ids:
        r = RiskAssessmentReasonRecord
        for assessment_id, code in session.execute(
            select(r.assessment_id, r.reason_code)
            .where(
                r.assessment_id.in_(ids),
                r.outcome.in_(["FAIL", "UNKNOWN", "HALT"]),
                r.reason_code.is_not(None),
            )
            .order_by(r.assessment_id, r.ordinal)
        ):
            if code is not None:
                decisive[assessment_id].append(code)
    return [_assessment(row) | {"reasons": decisive[row.id]} for row in rows]


def assessment(session: Session, assessment_id: uuid.UUID) -> dict[str, Any]:
    row = session.get(RiskAssessmentRecord, assessment_id)
    if row is None:
        raise NotFoundError(f"risk assessment {assessment_id} does not exist")
    checks = session.scalars(
        select(RiskAssessmentReasonRecord)
        .where(RiskAssessmentReasonRecord.assessment_id == assessment_id)
        .order_by(RiskAssessmentReasonRecord.ordinal)
    )
    return _assessment(row) | {
        "checks": [
            {
                "ordinal": c.ordinal,
                "check": c.check_code,
                "outcome": c.outcome.value,
                "reason_code": c.reason_code,
                "message": c.message,
                "measured": _s(c.measured),
                "limit": _s(c.limit_value),
                "unit": c.unit,
                "details": c.details,
            }
            for c in checks
        ],
        "calculations": row.calculations,
        "exposure_after": row.exposure_after,
        "inputs": row.inputs,
        "inputs_sha256": row.inputs_sha256,
        "content_sha256": row.content_sha256,
        "exposure_snapshot_id": None
        if row.exposure_snapshot_id is None
        else str(row.exposure_snapshot_id),
        "note": NOT_AN_APPROVAL,
    }


def assessment_counts(session: Session, account: PortfolioAccount) -> dict[str, int]:
    a = RiskAssessmentRecord
    counts = {r.value: 0 for r in AssessmentResult}
    for result, count in session.execute(
        select(a.result, func.count())
        .where(a.portfolio_account_id == account.id)
        .group_by(a.result)
    ):
        counts[result.value] = count
    return counts


def events(session: Session, account: PortfolioAccount, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RiskEventRecord)
        .where(RiskEventRecord.portfolio_account_id == account.id)
        .order_by(RiskEventRecord.occurred_at.desc(), RiskEventRecord.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": r.id,
            "event_type": r.event_type.value,
            "severity": r.severity.value,
            "occurred_at": _t(r.occurred_at),
            "actor": r.actor,
            "subject_kind": r.subject_kind,
            "subject_id": r.subject_id,
            "summary": r.summary,
            "details": r.details,
        }
        for r in rows
    ]


def live(state: EvaluatedState) -> dict[str, Any]:
    """The live evaluation (nothing recorded): figures, breakdown, conditions, baselines."""
    v = state.view

    def share(value: Decimal | None) -> str | None:
        if value is None or v.equity <= 0:
            return None
        return str(value / v.equity)

    return {
        "as_of": _t(state.as_of),
        "recorded": False,
        "mark_status": v.mark_status.value,
        "mark_issues": v.mark_issues,
        "ledger_sequence": v.ledger_sequence,
        "functional_currency": v.functional_currency,
        "equity": _s(v.equity),
        "contributed_capital": _s(v.contributed_capital),
        "cash": _s(v.cash),
        "available_capital": _s(v.available_capital),
        "long_exposure": _s(v.long_exposure),
        "short_exposure": _s(v.short_exposure),
        "gross_exposure": _s(v.gross_exposure),
        "net_exposure": _s(v.net_exposure),
        "margin_used": _s(v.margin_used),
        "pending_exposure": _s(v.pending_exposure),
        "pending_source": v.pending_source,
        "of_equity": {
            "long_exposure": share(v.long_exposure),
            "short_exposure": share(v.short_exposure),
            "gross_exposure": share(v.gross_exposure),
            "net_exposure": share(v.net_exposure),
            "margin_used": share(v.margin_used),
        },
        "open_positions": len(v.positions),
        "daily_loss": _s(v.daily_loss),
        "daily_baseline": _s(v.daily_baseline),
        "weekly_loss": _s(v.weekly_loss),
        "weekly_baseline": _s(v.weekly_baseline),
        "peak_equity": _s(v.peak_equity),
        "drawdown": _s(v.drawdown),
        "available_risk_budget": _s(v.available_risk_budget),
        "baselines": {
            kind.value: None
            if b is None
            else {
                "period": b.label,
                "period_start": _t(b.period_start),
                "equity": _s(b.equity),
                "basis": b.basis.value,
                "observed_at": _t(b.observed_at),
                "source": f"{b.source_kind}:{b.source_id}",
            }
            for kind, b in state.baselines.items()
        },
        "reconciliation": {
            "status": v.reconciliation_status,
            "current": v.reconciliation_current,
            "run_id": None
            if state.reconciliation_run_id is None
            else str(state.reconciliation_run_id),
        },
        "conditions": [c.measurements() | {"breaker": c.breaker.value} for c in state.conditions],
        "breakdown": breakdown(state),
    }


def overview(
    session: Session, account: PortfolioAccount, state: EvaluatedState | None
) -> dict[str, Any]:
    statuses = breaker_statuses(session, account.id)
    active = [b.value for b, s in statuses.items() if s is BreakerStatus.TRIPPED]
    return {
        "account": {
            "id": str(account.id),
            "name": account.name,
            "purpose": account.purpose.value,
            "base_currency": account.base_currency,
        },
        "trading_halted": bool(active),
        "active_breakers": active,
        "kill_switch": statuses[BreakerType.KILL_SWITCH].value,
        "breakers": breakers(session, account),
        "limits": limits(session, account),
        "live": None if state is None else live(state),
        "latest_snapshot": latest_snapshot(session, account),
        "assessment_counts": assessment_counts(session, account),
        "reserved_exposure": {
            "amount": "0",
            "note": "risk reservations do not exist before Phase 6C",
        },
        "disclaimer": PAPER_NOTE,
    }


__all__ = [
    "assessment",
    "assessment_counts",
    "assessments",
    "breaker_events",
    "breakers",
    "configurations",
    "events",
    "latest_snapshot",
    "limits",
    "live",
    "overview",
    "snapshots",
]

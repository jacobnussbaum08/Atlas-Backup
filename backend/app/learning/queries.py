"""Read models of the validation laboratory for the API and the dashboard (Phase 8B).

Every number returned here is read from stored records; nothing is recomputed or estimated,
except the live 5A model comparison (:func:`model_comparison`), which reads stored 5A runs.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clocks import Clock
from app.core.exceptions import NotFoundError
from app.database.models import MLModelVersionRecord
from app.database.models.learning import (
    LearningApprovalRecord,
    LearningCandidateEventRecord,
    LearningCandidateRecord,
    LearningDegradationAlertRecord,
    LearningDriftMeasurementRecord,
    LearningMonitoringRuleRecord,
    ValidationComparisonRecord,
    ValidationExperimentRecord,
    ValidationFoldRecord,
    ValidationHoldoutRecord,
    ValidationRobustnessRecord,
    ValidationRunRecord,
    ValidationSegmentRecord,
    ValidationTrialRecord,
)
from app.learning import governance as gov
from app.learning import models as model_eval
from app.learning.enums import (
    CandidateStatus,
    ExperimentKind,
    RobustnessMethod,
    RunStatus,
    SearchMethod,
    SubjectKind,
)
from app.learning.evaluation import OBJECTIVES
from app.learning.monitoring import DEFAULT_RULE
from app.learning.search import DEFAULT_SPACES
from app.learning.windows import WindowSpec
from app.ml_engine.registry import lifecycle as ml
from app.strategies.registry import StrategyRegistry

NOTICES = [
    "Historical simulation only: validation replays stored data through the paper-trading "
    "rules. Backtest and out-of-sample figures are not live paper-trading results.",
    "Out-of-sample means data not used to choose the parameters. It is not evidence of "
    "future performance, and a small sample stays insufficient evidence.",
    "Nothing here is promoted automatically: every lifecycle change is an operator decision "
    "with a reason, recorded with its evidence.",
    "The monitor can warn, and can suspend a strategy only under an operator-approved rule. "
    "It never changes risk limits or approves anything.",
]


def _iso(value: Any) -> Any:
    return None if value is None else value.isoformat()


def _run_row(run: ValidationRunRecord, experiment: ValidationExperimentRecord) -> dict[str, Any]:
    summary = run.summary or {}
    oos = summary.get("out_of_sample") or {}
    stats = oos.get("trade_statistics") or {}
    return {
        "id": str(run.id),
        "experiment_id": str(experiment.id),
        "name": experiment.name,
        "kind": experiment.kind.value,
        "strategies": experiment.strategy_ids,
        "search_method": experiment.search_method.value,
        "status": run.status.value,
        "progress": run.progress,
        "start_date": _iso(run.start_date),
        "end_date": _iso(run.end_date),
        "holdout_start": _iso(run.holdout_start),
        "holdout_end": _iso(run.holdout_end),
        "data_known_at": _iso(run.data_known_at),
        "verdict": None if run.verdict is None else run.verdict.value,
        "findings": run.findings,
        "trials": summary.get("trials_recorded"),
        "prior_trials": run.prior_trials,
        "oos_net_return": oos.get("net_return"),
        "oos_sharpe": oos.get("sharpe_annualized"),
        "oos_max_drawdown": oos.get("max_drawdown"),
        "oos_trades": stats.get("trades"),
        "oos_sessions": oos.get("sessions"),
        "reproduction_of": None if run.reproduction_of is None else str(run.reproduction_of),
        "reproduced_identical": run.reproduced_identical,
        "result_sha256": run.result_sha256,
        "error_message": run.error_message,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "requested_by": run.requested_by,
    }


def _load(
    session: Session, run_id: uuid.UUID
) -> tuple[ValidationRunRecord, ValidationExperimentRecord]:
    run = session.get(ValidationRunRecord, run_id)
    if run is None:
        raise NotFoundError(f"validation run {run_id} does not exist")
    experiment = session.get(ValidationExperimentRecord, run.experiment_id)
    assert experiment is not None  # noqa: S101  # foreign key
    return run, experiment


# ---------------------------------------------------------------------- overview
def overview(session: Session, clock: Clock) -> dict[str, Any]:
    r = ValidationRunRecord
    statuses = dict(session.execute(select(r.status, func.count()).group_by(r.status)).all())
    verdicts = dict(
        session.execute(
            select(r.verdict, func.count()).where(r.verdict.is_not(None)).group_by(r.verdict)
        ).all()
    )
    trials = session.scalar(select(func.count(ValidationTrialRecord.id))) or 0
    holdouts = session.scalar(select(func.count(ValidationHoldoutRecord.id))) or 0
    since = clock.now() - timedelta(days=7)
    alerts = (
        session.scalar(
            select(func.count(LearningDegradationAlertRecord.id)).where(
                LearningDegradationAlertRecord.created_at >= since
            )
        )
        or 0
    )
    candidate_status: Counter[str] = Counter()
    for cand in session.scalars(select(LearningCandidateRecord)):
        candidate_status[gov.status_of(session, cand.id).value] += 1
    return {
        "notices": NOTICES,
        "runs_by_status": {k.value: v for k, v in statuses.items()},
        "runs_by_verdict": {k.value: v for k, v in verdicts.items() if k is not None},
        "trials_recorded": trials,
        "holdout_evaluations": holdouts,
        "candidates_by_status": dict(candidate_status),
        "alerts_last_7_days": alerts,
    }


def options(registry: StrategyRegistry) -> dict[str, Any]:
    return {
        "strategies": [
            {
                "strategy_id": s.strategy_id,
                "family": s.family.value,
                "name": s.name,
                "default_space": None
                if DEFAULT_SPACES.get(s.family.value.lower()) is None
                else DEFAULT_SPACES[s.family.value.lower()].model_dump(mode="json"),
            }
            for s in registry
        ],
        "kinds": [k.value for k in ExperimentKind],
        "search_methods": [m.value for m in SearchMethod],
        "objectives": list(OBJECTIVES),
        "windows": WindowSpec().model_dump(mode="json"),
    }


# ---------------------------------------------------------------------- runs
def runs(
    session: Session,
    *,
    limit: int = 50,
    status: RunStatus | None = None,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    r, e = ValidationRunRecord, ValidationExperimentRecord
    q = select(r, e).join(e, e.id == r.experiment_id).order_by(r.created_at.desc()).limit(limit)
    if status is not None:
        q = q.where(r.status == status)
    if strategy is not None:
        q = q.where(e.strategy_ids.contains([strategy]))
    return [_run_row(run, exp) for run, exp in session.execute(q).all()]


def run_detail(session: Session, run_id: uuid.UUID) -> dict[str, Any]:
    run, experiment = _load(session, run_id)
    holdouts = session.scalars(
        select(ValidationHoldoutRecord)
        .where(ValidationHoldoutRecord.run_id == run.id)
        .order_by(ValidationHoldoutRecord.created_at)
    ).all()
    return {
        **_run_row(run, experiment),
        "config": experiment.config,
        "config_sha256": experiment.config_sha256,
        "base_config_sha256": run.base_config_sha256,
        "engine_version": run.engine_version,
        "code_version": run.code_version,
        "seed": run.seed,
        "layout": run.layout,
        "summary": run.summary,
        "strategy_versions": [
            {
                "strategy_id": s["strategy_id"],
                "version": s["version"],
                "logic_version": s["logic_version"],
                "parameters_sha256": s["parameters_sha256"],
            }
            for s in run.base_config.get("strategies", [])
        ],
        "risk_source": run.base_config.get("risk_source"),
        "execution_model_sha256": run.base_config.get("execution_model_sha256"),
        "universe": run.base_config.get("universe"),
        "assets": [a["symbol"] for a in run.base_config.get("assets", [])],
        "benchmark": (run.base_config.get("benchmark") or {}).get("symbol"),
        "holdout_evaluations": [holdout_row(h) for h in holdouts],
        "holdout_status": "UNTOUCHED"
        if not holdouts
        else (
            "EVALUATED_ONCE" if len(holdouts) == 1 and holdouts[0].access_number == 1 else "REUSED"
        ),
    }


def holdout_row(h: ValidationHoldoutRecord) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "run_id": str(h.run_id),
        "strategies": h.strategy_ids,
        "holdout_start": _iso(h.holdout_start),
        "holdout_end": _iso(h.holdout_end),
        "access_number": h.access_number,
        "parameters_sha256": h.parameters_sha256,
        "result": h.result,
        "config_sha256": h.config_sha256,
        "dataset_sha256": h.dataset_sha256,
        "requested_by": h.requested_by,
        "created_at": _iso(h.created_at),
    }


def trials(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    _load(session, run_id)
    t = ValidationTrialRecord
    return [
        {
            "trial_number": row.trial_number,
            "strategy_id": row.strategy_id,
            "source": row.source.value,
            "status": row.status.value,
            "selectable": row.selectable,
            "parameters": row.parameters,
            "parameters_sha256": row.parameters_sha256,
            "development": row.development,
            "error": row.error,
        }
        for row in session.scalars(select(t).where(t.run_id == run_id).order_by(t.trial_number))
    ]


def folds(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    _load(session, run_id)
    f = ValidationFoldRecord
    return [
        {
            "fold_index": row.fold_index,
            "refit": row.refit,
            "train": [_iso(row.train_start), _iso(row.train_end)],
            "validation": None
            if row.validation_start is None
            else [_iso(row.validation_start), _iso(row.validation_end)],
            "test": [_iso(row.test_start), _iso(row.test_end)],
            "embargo_sessions": row.embargo_sessions,
            "selected_trial": row.selected_trial,
            "selection": row.selection,
            "in_sample": row.in_sample,
            "validation_metrics": row.validation,
            "out_of_sample": row.out_of_sample,
            "champion_out_of_sample": row.champion_out_of_sample,
            "oos_series": row.oos_series,
            "champion_series": row.champion_series,
            "oos_config_sha256": row.oos_config_sha256,
            "oos_dataset_sha256": row.oos_dataset_sha256,
        }
        for row in session.scalars(select(f).where(f.run_id == run_id).order_by(f.fold_index))
    ]


def robustness(
    session: Session, run_id: uuid.UUID, method: RobustnessMethod | None = None
) -> list[dict[str, Any]]:
    _load(session, run_id)
    r = ValidationRobustnessRecord
    q = select(r).where(r.run_id == run_id).order_by(r.id)
    if method is not None:
        q = q.where(r.method == method)
    return [
        {
            "method": row.method.value,
            "scope": row.scope,
            "status": row.status.value,
            "result": row.result,
            "assumptions": row.assumptions,
            "limitations": row.limitations,
        }
        for row in session.scalars(q)
    ]


def segments(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    _load(session, run_id)
    s = ValidationSegmentRecord
    return [
        {
            "sample": row.sample.value,
            "segment_kind": row.segment_kind.value,
            "segment_value": row.segment_value,
            "metrics": row.metrics,
            "status": row.status.value,
        }
        for row in session.scalars(select(s).where(s.run_id == run_id).order_by(s.id))
    ]


def compare_runs(session: Session, run_ids: list[uuid.UUID]) -> dict[str, Any]:
    rows = []
    for rid in run_ids:
        run, experiment = _load(session, rid)
        summary = run.summary or {}
        rows.append(
            {
                **_run_row(run, experiment),
                "out_of_sample": summary.get("out_of_sample"),
                "champion_out_of_sample": summary.get("champion_out_of_sample"),
                "final_parameters": summary.get("final_parameters"),
            }
        )
    comparable = len({(r["start_date"], r["end_date"], r["holdout_start"]) for r in rows}) <= 1
    return {
        "runs": rows,
        "comparable_periods": comparable,
        "note": "runs over different periods or holdouts are not directly comparable",
    }


# ---------------------------------------------------------------------- models
def model_comparison(session: Session) -> list[dict[str, Any]]:
    return model_eval.compare(session)


def comparisons(
    session: Session, kind: SubjectKind | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    c = ValidationComparisonRecord
    q = select(c).order_by(c.created_at.desc()).limit(limit)
    if kind is not None:
        q = q.where(c.kind == kind)
    return [
        {
            "id": str(row.id),
            "kind": row.kind.value,
            "title": row.title,
            "subjects": row.subjects,
            "result": row.result,
            "result_sha256": row.result_sha256,
            "created_by": row.created_by,
            "created_at": _iso(row.created_at),
        }
        for row in session.scalars(q)
    ]


# ---------------------------------------------------------------------- governance
def _candidate_row(session: Session, cand: LearningCandidateRecord) -> dict[str, Any]:
    status = gov.status_of(session, cand.id)
    return {
        "id": str(cand.id),
        "subject_kind": cand.subject_kind.value,
        "subject_key": cand.subject_key,
        "status": status.value,
        "allowed_transitions": sorted(s.value for s in gov.TRANSITIONS[status])
        + (
            ["APPROVED (rollback)"]
            if status is CandidateStatus.RETIRED and gov.was_approved(session, cand.id)
            else []
        ),
        "parameters": cand.parameters,
        "parameters_sha256": cand.parameters_sha256,
        "model_version_id": None if cand.model_version_id is None else str(cand.model_version_id),
        "source_run_id": None if cand.source_run_id is None else str(cand.source_run_id),
        "champion_at_creation": cand.champion_at_creation,
        "notes": cand.notes,
        "created_by": cand.created_by,
        "created_at": _iso(cand.created_at),
    }


def candidates(session: Session, kind: SubjectKind | None = None) -> list[dict[str, Any]]:
    c = LearningCandidateRecord
    q = select(c).order_by(c.created_at.desc())
    if kind is not None:
        q = q.where(c.subject_kind == kind)
    return [_candidate_row(session, cand) for cand in session.scalars(q)]


def candidate_detail(session: Session, candidate_id: uuid.UUID) -> dict[str, Any]:
    cand = session.get(LearningCandidateRecord, candidate_id)
    if cand is None:
        raise NotFoundError(f"candidate {candidate_id} does not exist")
    e = LearningCandidateEventRecord
    events = session.scalars(select(e).where(e.candidate_id == cand.id).order_by(e.id)).all()
    return {
        **_candidate_row(session, cand),
        "champion_now": gov.champion(session, cand.subject_kind, cand.subject_key).as_json(),
        "events": [
            {
                "id": ev.id,
                "from_status": None if ev.from_status is None else ev.from_status.value,
                "to_status": ev.to_status.value,
                "reason": ev.reason,
                "actor": ev.actor,
                "actor_kind": ev.actor_kind.value,
                "evidence": ev.evidence,
                "created_at": _iso(ev.created_at),
            }
            for ev in events
        ],
        "approvals": approvals(session, candidate_id=cand.id),
        "shadow_evaluations": [
            c
            for c in comparisons(session, cand.subject_kind)
            if c["subjects"].get("candidate_id") == str(cand.id)
        ],
    }


def champions(session: Session, registry: StrategyRegistry) -> list[dict[str, Any]]:
    out = []
    for s in registry:
        champ = gov.champion(session, SubjectKind.STRATEGY, s.strategy_id).as_json()
        challengers = [
            _candidate_row(session, c)
            for c in gov.candidates_of(session, SubjectKind.STRATEGY, s.strategy_id)
        ]
        out.append(
            {**champ, "challengers": [c for c in challengers if c["status"] not in ("APPROVED",)]}
        )
    names = sorted(set(session.scalars(select(MLModelVersionRecord.model_name))))
    for name in names:
        champ = gov.champion(session, SubjectKind.MODEL, name).as_json()
        challengers = [
            _candidate_row(session, c) for c in gov.candidates_of(session, SubjectKind.MODEL, name)
        ]
        statuses = Counter(
            ml.current_status(session, mid).value
            for mid in session.scalars(
                select(MLModelVersionRecord.id).where(MLModelVersionRecord.model_name == name)
            )
        )
        out.append(
            {
                **champ,
                "registry_statuses": dict(statuses),
                "challengers": [c for c in challengers if c["status"] != "APPROVED"],
            }
        )
    return out


def approvals(
    session: Session, *, candidate_id: uuid.UUID | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    a = LearningApprovalRecord
    q = select(a).order_by(a.created_at.desc()).limit(limit)
    if candidate_id is not None:
        q = q.where(a.candidate_id == candidate_id)
    rows = []
    for row in session.scalars(q):
        cand = session.get(LearningCandidateRecord, row.candidate_id)
        rows.append(
            {
                "id": str(row.id),
                "candidate_id": str(row.candidate_id),
                "subject_kind": None if cand is None else cand.subject_kind.value,
                "subject_key": None if cand is None else cand.subject_key,
                "decision": row.decision.value,
                "approver": row.approver,
                "reason": row.reason,
                "evidence": row.evidence,
                "replaced": row.replaced,
                "strategy_version_id": None
                if row.strategy_version_id is None
                else str(row.strategy_version_id),
                "model_version_id": None
                if row.model_version_id is None
                else str(row.model_version_id),
                "created_at": _iso(row.created_at),
            }
        )
    return rows


# ---------------------------------------------------------------------- monitoring
def monitoring(session: Session, limit: int = 500) -> dict[str, Any]:
    m = LearningDriftMeasurementRecord
    latest = session.scalar(select(m.monitor_run_id).order_by(m.id.desc()).limit(1))
    measurements = []
    if latest is not None:
        measurements = [
            {
                "id": row.id,
                "subject_kind": row.subject_kind.value,
                "subject_key": row.subject_key,
                "metric": row.metric.value,
                "feature": row.feature,
                "measured_at": _iso(row.measured_at),
                "value": None if row.value is None else str(row.value),
                "p_value": None if row.p_value is None else str(row.p_value),
                "status": row.status.value,
                "n_reference": row.n_reference,
                "n_current": row.n_current,
                "reference": row.reference,
                "current": row.current,
                "thresholds": row.thresholds,
                "details": row.details,
            }
            for row in session.scalars(
                select(m).where(m.monitor_run_id == latest).order_by(m.id).limit(limit)
            )
        ]
    r = LearningMonitoringRuleRecord
    rules = [
        {
            "id": str(row.id),
            "subject_kind": row.subject_kind.value,
            "subject_key": row.subject_key,
            "version": row.version,
            "rule": row.rule,
            "action": row.action.value,
            "active": row.active,
            "approved_by": row.approved_by,
            "reason": row.reason,
            "created_at": _iso(row.created_at),
        }
        for row in session.scalars(select(r).order_by(r.created_at.desc()))
    ]
    return {
        "latest_monitor_run_id": None if latest is None else str(latest),
        "measurements": measurements,
        "rules": rules,
        "default_rule": DEFAULT_RULE.model_dump(),
        "alerts": alerts(session),
    }


def alerts(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    a = LearningDegradationAlertRecord
    return [
        {
            "id": str(row.id),
            "subject_kind": row.subject_kind.value,
            "subject_key": row.subject_key,
            "severity": row.severity,
            "metric": row.metric.value,
            "message": row.message,
            "action": row.action.value,
            "action_reference": row.action_reference,
            "rule_id": None if row.rule_id is None else str(row.rule_id),
            "created_at": _iso(row.created_at),
        }
        for row in session.scalars(select(a).order_by(a.created_at.desc()).limit(limit))
    ]


__all__ = [
    "NOTICES",
    "alerts",
    "approvals",
    "candidate_detail",
    "candidates",
    "champions",
    "compare_runs",
    "comparisons",
    "folds",
    "holdout_row",
    "model_comparison",
    "monitoring",
    "options",
    "overview",
    "robustness",
    "run_detail",
    "runs",
    "segments",
    "trials",
]

"""Read models of the ML engine for the API and dashboard (Phase 5A).

Everything returned is read from the database as recorded: no metric is computed here and
none is invented. Fixture datasets keep their TEST_FIXTURE origin in every view.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.database.models import (
    Asset,
    MLCalibrationResultRecord,
    MLDatasetRecord,
    MLDatasetVersionRecord,
    MLModelArtifactRecord,
    MLModelEvaluationRecord,
    MLModelStatusEventRecord,
    MLModelVersionRecord,
    MLPredictionRecord,
    MLTrainingRunRecord,
)
from app.ml_engine.enums import LifecycleStatus, SegmentKind, SplitKind, TaskKind
from app.ml_engine.registry import lifecycle

NOTES = [
    "Models produce analytical estimates only: no model can submit orders, size positions, "
    "approve risk or change risk settings.",
    "Every status change is an explicit operator action; nothing is promoted automatically.",
    "TEST_FIXTURE datasets and models were built from synthetic test data, not market data.",
    "Scores are probabilities only when the model's output kind is CALIBRATED_PROBABILITY.",
]


class DatasetVersionSummary(BaseModel):
    id: uuid.UUID
    dataset: str
    version: int
    source: str
    origin: str
    target_kind: str
    timeframes: list[str]
    asset_count: int
    observation_count: int
    matured_count: int
    decision_start: datetime | None
    decision_end: datetime | None
    data_known_at: datetime
    warnings: list[str]
    content_sha256: str
    created_at: datetime


class DatasetVersionDetail(DatasetVersionSummary):
    spec: dict[str, Any]
    feature_schema: list[dict[str, Any]]
    target_definition: dict[str, Any]
    statistics: dict[str, Any]
    code_version: dict[str, Any]
    runs: int


class RunSummary(BaseModel):
    id: uuid.UUID
    dataset_version_id: uuid.UUID
    dataset: str
    dataset_version: int
    origin: str
    model_type: str
    task: str
    status: str
    validation_status: str | None
    primary_metric: str
    test_metric: float | None
    test_baseline_metric: float | None
    test_observations: int | None
    test_mean_net_return: float | None
    test_take_all_net_return: float | None
    walk_forward_win_fraction: float | None
    test_period_reuse: int
    model_version_id: uuid.UUID | None
    model_name: str | None
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class RunDetail(RunSummary):
    spec: dict[str, Any]
    target_definition: dict[str, Any]
    code_version: dict[str, Any]
    random_seed: int
    periods: dict[str, Any] | None
    feature_schema: list[dict[str, Any]] | None
    preprocessing: dict[str, Any] | None
    parameters: dict[str, Any] | None
    training_metrics: dict[str, Any] | None
    validation_metrics: dict[str, Any] | None
    test_metrics: dict[str, Any] | None
    baseline_metrics: dict[str, Any] | None
    walk_forward: dict[str, Any] | None
    robustness: dict[str, Any] | None
    selection: dict[str, Any] | None
    validation_reasons: list[str]
    notes: list[str]
    evaluations: list[dict[str, Any]]
    calibration: list[dict[str, Any]]


class ModelSummary(BaseModel):
    id: uuid.UUID
    model_name: str
    version: int
    model_type: str
    task: str
    status: str
    status_changed_at: datetime | None
    output_kind: str
    calibration_method: str
    validation_status: str
    origin: str
    training_run_id: uuid.UUID
    dataset_version_id: uuid.UUID
    evaluation_summary: dict[str, Any]
    target_kind: str
    created_at: datetime


class StatusEventView(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    reason: str
    actor: str
    created_at: datetime


class ModelDetail(ModelSummary):
    input_schema: list[dict[str, Any]]
    target_definition: dict[str, Any]
    parameters: dict[str, Any]
    artifact: dict[str, Any]
    status_history: list[StatusEventView]
    allowed_transitions: list[str]
    predictions: int


class PredictionView(BaseModel):
    id: uuid.UUID
    model_version_id: uuid.UUID
    model_name: str
    model_version: int
    model_status: str
    mode: str
    symbol: str | None
    timeframe: str
    direction: str
    strategy_id: str | None
    decision_at: datetime
    predicted_at: datetime
    validity: str
    problems: list[str]
    output_kind: str
    output: dict[str, Any] | None
    requested_by: str


class PredictionDetail(PredictionView):
    feature_snapshot: dict[str, Any]
    feature_snapshot_sha256: str
    input_schema_sha256: str
    artifact_sha256: str
    inputs_available_at: datetime | None


def _version_summary(v: MLDatasetVersionRecord, name: str) -> dict[str, Any]:
    return {
        "id": v.id,
        "dataset": name,
        "version": v.version,
        "source": v.source.value,
        "origin": v.origin.value,
        "target_kind": v.target_kind.value,
        "timeframes": list(v.timeframes),
        "asset_count": v.asset_count,
        "observation_count": v.observation_count,
        "matured_count": v.matured_count,
        "decision_start": v.decision_start,
        "decision_end": v.decision_end,
        "data_known_at": v.data_known_at,
        "warnings": list(v.warnings),
        "content_sha256": v.content_sha256,
        "created_at": v.created_at,
    }


def dataset_versions(session: Session, limit: int = 100) -> list[DatasetVersionSummary]:
    rows = session.execute(
        select(MLDatasetVersionRecord, MLDatasetRecord.name)
        .join(MLDatasetRecord, MLDatasetRecord.id == MLDatasetVersionRecord.dataset_id)
        .order_by(MLDatasetVersionRecord.created_at.desc())
        .limit(limit)
    ).all()
    return [DatasetVersionSummary(**_version_summary(v, name)) for v, name in rows]


def dataset_version(session: Session, version_id: uuid.UUID) -> DatasetVersionDetail | None:
    row = session.execute(
        select(MLDatasetVersionRecord, MLDatasetRecord.name)
        .join(MLDatasetRecord, MLDatasetRecord.id == MLDatasetVersionRecord.dataset_id)
        .where(MLDatasetVersionRecord.id == version_id)
    ).first()
    if row is None:
        return None
    v, name = row
    runs = session.scalar(
        select(func.count()).where(MLTrainingRunRecord.dataset_version_id == v.id)
    )
    return DatasetVersionDetail(
        **_version_summary(v, name),
        spec=v.spec,
        feature_schema=v.feature_schema,
        target_definition=v.target_definition,
        statistics=v.statistics,
        code_version=v.code_version,
        runs=int(runs or 0),
    )


def _primary(task: TaskKind) -> str:
    return "log_loss" if task is TaskKind.CLASSIFICATION else "rmse"


def _run_summary(
    r: MLTrainingRunRecord,
    v: MLDatasetVersionRecord,
    name: str,
    model: MLModelVersionRecord | None,
) -> dict[str, Any]:
    key = _primary(r.task)
    test = r.test_metrics or {}
    base = (r.baseline_metrics or {}).get("test", {})
    fin = test.get("financial", {}) if isinstance(test, dict) else {}
    return {
        "id": r.id,
        "dataset_version_id": r.dataset_version_id,
        "dataset": name,
        "dataset_version": v.version,
        "origin": v.origin.value,
        "model_type": r.model_type.value,
        "task": r.task.value,
        "status": r.status.value,
        "validation_status": None if r.validation_status is None else r.validation_status.value,
        "primary_metric": key,
        "test_metric": test.get(key),
        "test_baseline_metric": base.get(key),
        "test_observations": test.get("observations"),
        "test_mean_net_return": fin.get("mean_net_return"),
        "test_take_all_net_return": fin.get("take_all_mean_net_return"),
        "walk_forward_win_fraction": (r.walk_forward or {}).get("win_fraction"),
        "test_period_reuse": r.test_period_reuse,
        "model_version_id": None if model is None else model.id,
        "model_name": None if model is None else model.model_name,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "error": r.error,
    }


def _runs_query() -> Select[MLTrainingRunRecord, MLDatasetVersionRecord, str, MLModelVersionRecord]:
    return (
        select(
            MLTrainingRunRecord, MLDatasetVersionRecord, MLDatasetRecord.name, MLModelVersionRecord
        )
        .join(
            MLDatasetVersionRecord,
            MLDatasetVersionRecord.id == MLTrainingRunRecord.dataset_version_id,
        )
        .join(MLDatasetRecord, MLDatasetRecord.id == MLDatasetVersionRecord.dataset_id)
        .outerjoin(
            MLModelVersionRecord, MLModelVersionRecord.training_run_id == MLTrainingRunRecord.id
        )
    )


def runs(
    session: Session,
    *,
    status: str | None = None,
    model_type: str | None = None,
    dataset_version_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[RunSummary]:
    stmt = _runs_query()
    if status:
        stmt = stmt.where(MLTrainingRunRecord.status == status)
    if model_type:
        stmt = stmt.where(MLTrainingRunRecord.model_type == model_type)
    if dataset_version_id:
        stmt = stmt.where(MLTrainingRunRecord.dataset_version_id == dataset_version_id)
    rows = session.execute(stmt.order_by(MLTrainingRunRecord.started_at.desc()).limit(limit)).all()
    return [RunSummary(**_run_summary(r, v, name, model)) for r, v, name, model in rows]


def run(session: Session, run_id: uuid.UUID) -> RunDetail | None:
    row = session.execute(_runs_query().where(MLTrainingRunRecord.id == run_id)).first()
    if row is None:
        return None
    r, v, name, model = row
    evaluations = session.scalars(
        select(MLModelEvaluationRecord)
        .where(MLModelEvaluationRecord.training_run_id == run_id)
        .order_by(MLModelEvaluationRecord.id)
    ).all()
    calibration = session.scalars(
        select(MLCalibrationResultRecord)
        .where(MLCalibrationResultRecord.training_run_id == run_id)
        .order_by(MLCalibrationResultRecord.id)
    ).all()
    return RunDetail(
        **_run_summary(r, v, name, model),
        spec=r.spec,
        target_definition=r.target_definition,
        code_version=r.code_version,
        random_seed=r.random_seed,
        periods=r.periods,
        feature_schema=r.feature_schema,
        preprocessing=r.preprocessing,
        parameters=r.parameters,
        training_metrics=r.training_metrics,
        validation_metrics=r.validation_metrics,
        test_metrics=r.test_metrics,
        baseline_metrics=r.baseline_metrics,
        walk_forward=r.walk_forward,
        robustness=r.robustness,
        selection=r.selection,
        validation_reasons=list(r.validation_reasons),
        notes=list(r.notes),
        evaluations=[
            {
                "split": e.split.value,
                "fold": e.fold,
                "segment_kind": e.segment_kind.value,
                "segment_value": e.segment_value,
                "period_start": e.period_start,
                "period_end": e.period_end,
                "observations": e.observations,
                "metrics": e.metrics,
                "baseline_metrics": e.baseline_metrics,
            }
            for e in evaluations
        ],
        calibration=[
            {
                "method": c.method.value,
                "fitted_on": c.fitted_on.value,
                "evaluated_on": c.evaluated_on,
                "observations": c.observations,
                "brier_raw": c.brier_raw,
                "brier_calibrated": c.brier_calibrated,
                "brier_base_rate": c.brier_base_rate,
                "ece_raw": c.ece_raw,
                "ece_calibrated": c.ece_calibrated,
                "reliability_raw": c.reliability_raw,
                "reliability_calibrated": c.reliability_calibrated,
                "selected": c.selected,
                "accepted": c.accepted,
                "decision": c.decision,
            }
            for c in calibration
        ],
    )


def _status_times(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    if not ids:
        return {}
    e = MLModelStatusEventRecord
    rows = session.execute(
        select(e.model_version_id, func.max(e.created_at))
        .where(e.model_version_id.in_(ids))
        .group_by(e.model_version_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def _model_summary(
    m: MLModelVersionRecord, status: LifecycleStatus, changed: datetime | None
) -> dict[str, Any]:
    return {
        "id": m.id,
        "model_name": m.model_name,
        "version": m.version,
        "model_type": m.model_type.value,
        "task": m.task.value,
        "status": status.value,
        "status_changed_at": changed,
        "output_kind": m.output_kind.value,
        "calibration_method": m.calibration_method.value,
        "validation_status": m.validation_status.value,
        "origin": m.origin.value,
        "training_run_id": m.training_run_id,
        "dataset_version_id": m.dataset_version_id,
        "evaluation_summary": m.evaluation_summary,
        "target_kind": str(m.target_definition.get("kind")),
        "created_at": m.created_at,
    }


def models(session: Session, *, status: str | None = None, limit: int = 100) -> list[ModelSummary]:
    rows = list(
        session.scalars(
            select(MLModelVersionRecord)
            .order_by(MLModelVersionRecord.created_at.desc())
            .limit(limit)
        )
    )
    ids = [m.id for m in rows]
    current = lifecycle.statuses(session, ids)
    times = _status_times(session, ids)
    out = [
        ModelSummary(**_model_summary(m, current[m.id], times.get(m.id)))
        for m in rows
        if m.id in current
    ]
    return [o for o in out if status is None or o.status == status]


def model(session: Session, model_id: uuid.UUID) -> ModelDetail | None:
    m = session.get(MLModelVersionRecord, model_id)
    if m is None:
        return None
    status = lifecycle.current_status(session, m.id)
    events = session.scalars(
        select(MLModelStatusEventRecord)
        .where(MLModelStatusEventRecord.model_version_id == m.id)
        .order_by(MLModelStatusEventRecord.id)
    ).all()
    artifact = session.get(MLModelArtifactRecord, m.artifact_id)
    predictions = session.scalar(
        select(func.count()).where(MLPredictionRecord.model_version_id == m.id)
    )
    return ModelDetail(
        **_model_summary(m, status, events[-1].created_at if events else None),
        input_schema=m.input_schema,
        target_definition=m.target_definition,
        parameters=m.parameters,
        artifact={}
        if artifact is None
        else {
            "path": artifact.path,
            "format": artifact.format,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "libraries": artifact.libraries,
        },
        status_history=[
            StatusEventView(
                id=e.id,
                from_status=None if e.from_status is None else e.from_status.value,
                to_status=e.to_status.value,
                reason=e.reason,
                actor=e.actor,
                created_at=e.created_at,
            )
            for e in events
        ],
        allowed_transitions=sorted(s.value for s in lifecycle.TRANSITIONS[status]),
        predictions=int(predictions or 0),
    )


def compare(session: Session, run_ids: list[uuid.UUID]) -> dict[str, Any]:
    """Side by side test-period evidence of several runs, with comparability flags."""
    details = [d for d in (run(session, i) for i in run_ids) if d is not None]
    datasets = {d.dataset_version_id for d in details}
    tests = {
        (
            (d.periods or {}).get("test", {}).get("start"),
            (d.periods or {}).get("test", {}).get("end"),
        )
        for d in details
    }
    tasks = {d.task for d in details}
    rows = []
    for d in details:
        test = d.test_metrics or {}
        rows.append(
            {
                "run_id": d.id,
                "model_type": d.model_type,
                "model_name": d.model_name,
                "status": d.status,
                "validation_status": d.validation_status,
                "primary_metric": d.primary_metric,
                "test_metric": d.test_metric,
                "test_baseline_metric": d.test_baseline_metric,
                "walk_forward_mean_metric": (d.walk_forward or {}).get("mean_metric"),
                "walk_forward_win_fraction": d.walk_forward_win_fraction,
                "test_observations": d.test_observations,
                "roc_auc": test.get("roc_auc"),
                "brier": test.get("brier"),
                "rmse": test.get("rmse"),
                "directional_accuracy": test.get("directional_accuracy"),
                "financial": test.get("financial"),
                "test_period": (d.periods or {}).get("test"),
                "origin": d.origin,
            }
        )
    notes = []
    if len(datasets) > 1:
        notes.append("Runs use different dataset versions: metrics are not directly comparable.")
    if len(tests) > 1:
        notes.append("Runs use different test periods: metrics are not directly comparable.")
    if len(tasks) > 1:
        notes.append("Runs mix classification and regression targets.")
    return {
        "runs": rows,
        "comparable": len(datasets) <= 1 and len(tests) <= 1 and len(tasks) <= 1,
        "missing": [str(i) for i in run_ids if i not in {d.id for d in details}],
        "notes": [
            *notes,
            "Choosing among runs by their test results turns the test period into a selection set.",
        ],
    }


def _prediction_view(
    p: MLPredictionRecord, m: MLModelVersionRecord, symbol: str | None
) -> dict[str, Any]:
    return {
        "id": p.id,
        "model_version_id": p.model_version_id,
        "model_name": m.model_name,
        "model_version": m.version,
        "model_status": p.model_status.value,
        "mode": p.mode.value,
        "symbol": symbol,
        "timeframe": p.timeframe,
        "direction": p.direction.value,
        "strategy_id": p.strategy_id,
        "decision_at": p.decision_at,
        "predicted_at": p.predicted_at,
        "validity": p.validity.value,
        "problems": list(p.problems),
        "output_kind": p.output_kind.value,
        "output": p.output,
        "requested_by": p.requested_by,
    }


def predictions(
    session: Session,
    *,
    model_version_id: uuid.UUID | None = None,
    asset_id: uuid.UUID | None = None,
    validity: str | None = None,
    limit: int = 50,
) -> list[PredictionView]:
    stmt = (
        select(MLPredictionRecord, MLModelVersionRecord, Asset.symbol)
        .join(MLModelVersionRecord, MLModelVersionRecord.id == MLPredictionRecord.model_version_id)
        .outerjoin(Asset, Asset.id == MLPredictionRecord.asset_id)
    )
    if model_version_id:
        stmt = stmt.where(MLPredictionRecord.model_version_id == model_version_id)
    if asset_id:
        stmt = stmt.where(MLPredictionRecord.asset_id == asset_id)
    if validity:
        stmt = stmt.where(MLPredictionRecord.validity == validity)
    rows = session.execute(stmt.order_by(MLPredictionRecord.predicted_at.desc()).limit(limit)).all()
    return [PredictionView(**_prediction_view(p, m, symbol)) for p, m, symbol in rows]


def prediction(session: Session, prediction_id: uuid.UUID) -> PredictionDetail | None:
    row = session.execute(
        select(MLPredictionRecord, MLModelVersionRecord, Asset.symbol)
        .join(MLModelVersionRecord, MLModelVersionRecord.id == MLPredictionRecord.model_version_id)
        .outerjoin(Asset, Asset.id == MLPredictionRecord.asset_id)
        .where(MLPredictionRecord.id == prediction_id)
    ).first()
    if row is None:
        return None
    p, m, symbol = row
    return PredictionDetail(
        **_prediction_view(p, m, symbol),
        feature_snapshot=p.feature_snapshot,
        feature_snapshot_sha256=p.feature_snapshot_sha256,
        input_schema_sha256=p.input_schema_sha256,
        artifact_sha256=p.artifact_sha256,
        inputs_available_at=p.inputs_available_at,
    )


def overview(session: Session) -> dict[str, Any]:
    run_counts = dict(
        session.execute(
            select(MLTrainingRunRecord.status, func.count()).group_by(MLTrainingRunRecord.status)
        ).all()
    )
    ids = list(session.scalars(select(MLModelVersionRecord.id)))
    current = lifecycle.statuses(session, ids)
    by_status: dict[str, int] = {s.value: 0 for s in LifecycleStatus}
    for status in current.values():
        by_status[status.value] += 1
    origins = dict(
        session.execute(
            select(MLDatasetVersionRecord.origin, func.count()).group_by(
                MLDatasetVersionRecord.origin
            )
        ).all()
    )
    return {
        "datasets": int(session.scalar(select(func.count()).select_from(MLDatasetRecord)) or 0),
        "dataset_versions": {k.value: v for k, v in origins.items()},
        "training_runs": {k.value: v for k, v in run_counts.items()},
        "models": by_status,
        "predictions": int(
            session.scalar(select(func.count()).select_from(MLPredictionRecord)) or 0
        ),
        "segments": [s.value for s in SegmentKind],
        "splits": [s.value for s in SplitKind],
        "notes": NOTES,
    }


__all__ = [
    "NOTES",
    "DatasetVersionDetail",
    "DatasetVersionSummary",
    "ModelDetail",
    "ModelSummary",
    "PredictionDetail",
    "PredictionView",
    "RunDetail",
    "RunSummary",
    "StatusEventView",
    "compare",
    "dataset_version",
    "dataset_versions",
    "model",
    "models",
    "overview",
    "prediction",
    "predictions",
    "run",
    "runs",
]

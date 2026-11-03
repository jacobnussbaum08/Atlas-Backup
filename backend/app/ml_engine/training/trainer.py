"""Training runs (Phase 5A).

One run trains one model family on one dataset version:

1. Load the version; keep the rows the run asks for (timeframes, directions, symbols,
   strategies) that are usable (matured label, finite values; ``valid_rows``).
2. Split chronologically by decision time into train / validation / test, purged and
   embargoed (``validation.splits``). The test period is untouched until step 6.
3. **Walk-forward** over the development period (train + validation): for each
   hyperparameter candidate, preprocessing and model are fitted on each fold's past and
   evaluated on its future. The candidate with the best mean fold score is selected; the
   number of candidates tried is recorded (a grid is capped to limit selection bias).
4. Fit preprocessing and the selected model on the training period only.
5. Classification: calibrate on the validation period only (``calibration.py``).
6. Evaluate once on the test period, against simple baselines, overall and by asset,
   timeframe, direction, strategy and market segment; supplementary purged k-fold and CPCV
   on the development period if requested.
7. Record the evidence verdict (``validation_status``), write the artifact and register a
   CANDIDATE model version. Nothing is promoted.

Every input, parameter, period, seed, library version and metric is stored with the run.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models import (
    MLCalibrationResultRecord,
    MLModelArtifactRecord,
    MLModelEvaluationRecord,
    MLModelVersionRecord,
    MLTrainingRunRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import sha256_json
from app.ml_engine.config import MLSettings
from app.ml_engine.datasets.spec import NAME_PATTERN
from app.ml_engine.datasets.store import DatasetFrame, load_frame
from app.ml_engine.enums import (
    CalibrationMethod,
    LifecycleStatus,
    ModelType,
    OutputKind,
    RunStatus,
    SegmentKind,
    SplitKind,
    TaskKind,
    ValidationStatus,
)
from app.ml_engine.features.preprocessing import (
    FittedPreprocessor,
    PreprocessingSpec,
    fit_preprocessor,
    valid_rows,
)
from app.ml_engine.registry import lifecycle
from app.ml_engine.registry.artifacts import ARTIFACT_FORMAT, write_artifact
from app.ml_engine.training import metrics as m
from app.ml_engine.training.calibration import (
    CalibrationDecision,
    CalibrationPolicy,
    Calibrator,
    calibrate,
)
from app.ml_engine.training.models import build_model, describe_family, parse_params
from app.ml_engine.validation.splits import (
    Fold,
    Split,
    SplitSpec,
    Timeline,
    chronological_split,
    cpcv,
    purged_kfold,
    walk_forward,
)
from app.ml_engine.versioning import code_version

logger = get_logger(__name__)

TRAINER_VERSION = "trainer-v1"
MAX_GRID = 12
Floats = NDArray[np.float64]
Mask = NDArray[np.bool_]


class TrainingError(ValueError):
    """The run cannot proceed (too little data, a single class, ...)."""


class EvidencePolicy(BaseModel):
    """What a PASSED validation status requires (spec 19.2: out-of-sample evidence)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_test_observations: int = Field(default=200, ge=10, le=1_000_000)
    min_walk_forward_win_fraction: float = Field(default=0.5, ge=0, le=1)


class TrainingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version_id: uuid.UUID
    model_type: ModelType
    model_name: str | None = Field(default=None, pattern=NAME_PATTERN)
    params: dict[str, Any] = Field(default_factory=dict)
    # Hyperparameter candidates (each completed with the defaults); selected by walk-forward
    # on the development period only. Empty: ``params`` alone.
    grid: tuple[dict[str, Any], ...] = ()
    seed: int = Field(default=20_260_101, ge=0, le=2**31 - 1)
    split: SplitSpec = SplitSpec()
    preprocessing: PreprocessingSpec = PreprocessingSpec()
    evidence: EvidencePolicy = EvidencePolicy()
    # Row filters (asset groups and the like). Empty: everything in the dataset.
    timeframes: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    # UNIQUENESS: weight each training observation by 1 / its label concurrency.
    sample_weighting: Literal["NONE", "UNIQUENESS"] = "UNIQUENESS"
    # Classification: score threshold of the "take" policy before calibration.
    decision_threshold: float = Field(default=0.5, gt=0, lt=1)
    # Also evaluate taking the top fraction of scores (threshold set on validation).
    top_fraction: float = Field(default=0.2, gt=0, le=1)

    @field_validator("grid")
    @classmethod
    def _grid(cls, value: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        if len(value) > MAX_GRID:
            raise ValueError(f"at most {MAX_GRID} hyperparameter candidates")
        return value

    def stored(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class Prepared:
    frame: pl.DataFrame
    data: DatasetFrame
    task: TaskKind
    y: Floats
    net: Floats
    line: Timeline
    weights: Floats
    rejected: dict[str, int]


def _task(target_definition: dict[str, Any]) -> TaskKind:
    return TaskKind(target_definition["task"])


def _uniqueness(frame: pl.DataFrame) -> Floats:
    """1 / number of labels (same asset, timeframe, direction, strategy) whose windows
    overlap each observation's window, itself included. Uses label spans only."""
    weights = np.ones(frame.height, dtype=np.float64)
    keys = frame.select(["asset_id", "timeframe", "direction", "strategy_id"]).rows()
    starts = frame["decision_at"].to_numpy()
    ends = frame["label_end_at"].to_numpy()
    groups: dict[tuple[Any, ...], list[int]] = {}
    for i, key in enumerate(keys):
        groups.setdefault(key, []).append(i)
    for members in groups.values():
        idx = np.array(members)
        s, e = starts[idx], ends[idx]
        for j, row in enumerate(idx):
            overlap = (s < e[j]) & (e > s[j])
            weights[row] = 1.0 / max(1, int(overlap.sum()))
    return weights


def prepare(session: Session, spec: TrainingSpec) -> Prepared:
    data = load_frame(session, spec.dataset_version_id)
    frame = data.frame
    filters: list[tuple[str, Sequence[str]]] = [
        ("timeframe", spec.timeframes),
        ("direction", spec.directions),
        ("symbol", [s.upper() for s in spec.symbols]),
        ("strategy_id", spec.strategies),
    ]
    for column, allowed in filters:
        if allowed:
            frame = frame.filter(pl.col(column).is_in(list(allowed)))
    keep, rejected = valid_rows(frame, data.numeric)
    frame = frame.filter(keep).sort(["decision_at", "row_index"])
    if frame.height < 50:
        raise TrainingError(f"only {frame.height} usable observations; at least 50 are needed")
    task = _task(data.version.target_definition)
    if task is TaskKind.CLASSIFICATION:
        y = frame["label_class"].cast(pl.Float64).to_numpy()
        if np.unique(y).size < 2:
            raise TrainingError("the usable observations contain a single class")
    else:
        y = frame["label_value"].cast(pl.Float64).to_numpy()
    line = Timeline(frame["decision_at"].to_numpy(), frame["label_end_at"].to_numpy())
    weights = (
        _uniqueness(frame)
        if spec.sample_weighting == "UNIQUENESS"
        else np.ones(frame.height, dtype=np.float64)
    )
    return Prepared(
        frame=frame,
        data=data,
        task=task,
        y=y.astype(np.float64),
        net=frame["net_return"].cast(pl.Float64).to_numpy().astype(np.float64),
        line=line,
        weights=weights,
        rejected=rejected,
    )


@dataclass
class Fitted:
    preprocessor: FittedPreprocessor
    model: Any
    task: TaskKind

    def score(self, frame: pl.DataFrame) -> Floats:
        x = self.preprocessor.transform(frame)
        if self.task is TaskKind.CLASSIFICATION:
            return np.asarray(self.model.predict_proba(x)[:, 1], dtype=np.float64)
        return np.asarray(self.model.predict(x), dtype=np.float64)


def fit(
    p: Prepared,
    mask: Mask,
    spec: TrainingSpec,
    params: dict[str, Any],
    n_jobs: int,
) -> Fitted:
    train = p.frame.filter(pl.Series(mask))
    y = p.y[mask]
    if p.task is TaskKind.CLASSIFICATION and np.unique(y).size < 2:
        raise TrainingError("a training period contains a single class")
    pre = fit_preprocessor(
        train,
        p.data.numeric,
        p.data.categorical,
        spec.preprocessing,
        model=spec.model_type,
        task=p.task,
        label=y,
        seed=spec.seed,
    )
    x = pre.transform(train)
    if x.shape[1] == 0:
        raise TrainingError("no feature survived preprocessing")
    model = build_model(spec.model_type, p.task, params, seed=spec.seed, n_jobs=n_jobs)
    weights = p.weights[mask]
    # Mean 1 within the training set: weights express relative uniqueness only, so
    # hyperparameters such as XGBoost's min_child_weight keep their usual scale.
    model.fit(x, y, sample_weight=weights / weights.mean())
    return Fitted(pre, model, p.task)


# --- Evaluation ------------------------------------------------------------------------


def _baseline_score(p: Prepared, train: Mask, n: int) -> Floats:
    """Classification: the training base rate; regression: the training mean."""
    return np.full(n, float(np.average(p.y[train], weights=p.weights[train])))


def _primary(task: TaskKind, metrics: dict[str, Any]) -> float | None:
    """Lower is better: log loss (classification) or RMSE (regression)."""
    key = "log_loss" if task is TaskKind.CLASSIFICATION else "rmse"
    value = metrics.get(key)
    return None if value is None else float(value)


def _stat_metrics(task: TaskKind, y: Floats, score: Floats, threshold: float) -> dict[str, Any]:
    if task is TaskKind.CLASSIFICATION:
        return m.classification_metrics(y, score, threshold)
    return m.regression_metrics(y, score)


def _take(task: TaskKind, score: Floats, threshold: float) -> Mask:
    if task is TaskKind.CLASSIFICATION:
        return score >= threshold
    return score > 0  # the label is already net of costs: a positive expected net outcome


def evaluate_block(
    p: Prepared,
    train: Mask,
    test: Mask,
    score: Floats,
    *,
    threshold: float,
    top_threshold: float | None,
    cost: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(model metrics, baseline metrics) on the ``test`` rows; ``score`` covers them."""
    y, net = p.y[test], p.net[test]
    base = _baseline_score(p, train, int(test.sum()))
    metrics = _stat_metrics(p.task, y, score, threshold)
    metrics["financial"] = m.financial_metrics(net, _take(p.task, score, threshold), cost)
    if top_threshold is not None:
        metrics["financial_top"] = m.financial_metrics(net, score >= top_threshold, cost)
    baseline = _stat_metrics(p.task, y, base, threshold)
    baseline["financial_take_all"] = m.financial_metrics(net, np.ones(y.size, bool), cost)
    baseline["name"] = (
        "training base rate (constant score)"
        if p.task is TaskKind.CLASSIFICATION
        else "training mean (constant prediction)"
    )
    return metrics, baseline


@dataclass
class FoldResult:
    fold: Fold
    metrics: dict[str, Any]
    baseline: dict[str, Any]


def run_folds(
    p: Prepared,
    folds: Sequence[Fold],
    spec: TrainingSpec,
    params: dict[str, Any],
    cost: float,
    n_jobs: int,
) -> list[FoldResult]:
    results = []
    for fold in folds:
        try:
            fitted = fit(p, fold.train, spec, params, n_jobs)
        except TrainingError as exc:
            results.append(FoldResult(fold, {"skipped": str(exc)}, {}))
            continue
        score = fitted.score(p.frame.filter(pl.Series(fold.test)))
        metrics, baseline = evaluate_block(
            p,
            fold.train,
            fold.test,
            score,
            threshold=spec.decision_threshold,
            top_threshold=None,
            cost=cost,
        )
        results.append(FoldResult(fold, metrics, baseline))
    return results


def summarize_folds(task: TaskKind, results: Sequence[FoldResult]) -> dict[str, Any]:
    scored = [r for r in results if "skipped" not in r.metrics]
    primary = [_primary(task, r.metrics) for r in scored]
    base = [_primary(task, r.baseline) for r in scored]
    pairs = [(a, b) for a, b in zip(primary, base, strict=True) if a is not None and b is not None]
    wins = sum(1 for a, b in pairs if a < b)
    net = [r.metrics["financial"].get("mean_net_return") for r in scored]
    net_values = [float(v) for v in net if v is not None]
    return {
        "folds": len(results),
        "scored_folds": len(scored),
        "metric": "log_loss" if task is TaskKind.CLASSIFICATION else "rmse",
        "mean_metric": float(np.mean([a for a, _ in pairs])) if pairs else None,
        "mean_baseline_metric": float(np.mean([b for _, b in pairs])) if pairs else None,
        "folds_beating_baseline": wins,
        "win_fraction": wins / len(pairs) if pairs else None,
        "mean_selected_net_return": float(np.mean(net_values)) if net_values else None,
        "fold_details": [
            {
                "fold": r.fold.index,
                **r.fold.info,
                "train_observations": int(r.fold.train.sum()),
                "test_observations": int(r.fold.test.sum()),
                "metric": _primary(task, r.metrics) if "skipped" not in r.metrics else None,
                "baseline_metric": _primary(task, r.baseline) if r.baseline else None,
                "skipped": r.metrics.get("skipped"),
            }
            for r in results
        ],
    }


SEGMENT_COLUMNS: dict[SegmentKind, str] = {
    SegmentKind.ASSET: "symbol",
    SegmentKind.TIMEFRAME: "timeframe",
    SegmentKind.DIRECTION: "direction",
    SegmentKind.STRATEGY: "strategy_id",
    SegmentKind.MARKET_TREND: "market_trend",
    SegmentKind.VOLATILITY: "volatility",
}


def segment_metrics(
    p: Prepared,
    train: Mask,
    test: Mask,
    score: Floats,
    threshold: float,
    cost: float,
) -> dict[SegmentKind, dict[str, dict[str, Any]]]:
    rows = p.frame.filter(pl.Series(test))
    out: dict[SegmentKind, dict[str, dict[str, Any]]] = {}
    for kind, column in SEGMENT_COLUMNS.items():
        values = rows[column].cast(pl.String).fill_null("UNKNOWN").to_list()
        test_index = np.flatnonzero(test)

        def metric(
            mask: NDArray[np.bool_], test_index: NDArray[np.int64] = test_index
        ) -> dict[str, Any]:
            sub = np.zeros_like(test)
            sub[test_index[mask]] = True
            model, base = evaluate_block(
                p, train, sub, score[mask], threshold=threshold, top_threshold=None, cost=cost
            )
            return {"model": model, "baseline": base}

        out[kind] = m.segment_table(values, metric)
    return out


def evidence_verdict(
    task: TaskKind,
    test: dict[str, Any],
    baseline: dict[str, Any],
    walk: dict[str, Any],
    policy: EvidencePolicy,
) -> tuple[ValidationStatus, list[str]]:
    n = int(test.get("observations", 0))
    if n < policy.min_test_observations:
        return ValidationStatus.INSUFFICIENT_EVIDENCE, [
            f"{n} test observations (< {policy.min_test_observations})"
        ]
    reasons: list[str] = []
    model_metric, base_metric = _primary(task, test), _primary(task, baseline)
    name = "log loss" if task is TaskKind.CLASSIFICATION else "RMSE"
    if model_metric is None or base_metric is None or model_metric >= base_metric:
        reasons.append(f"test {name} does not beat the baseline")
    if task is TaskKind.CLASSIFICATION:
        auc = test.get("roc_auc")
        if auc is None or auc <= 0.5:
            reasons.append("test ROC-AUC is not above 0.5")
    else:
        da = test.get("directional_accuracy")
        if da is None or da <= 0.5:
            reasons.append("test directional accuracy is not above 0.5")
    fin = test.get("financial", {})
    mean_net = fin.get("mean_net_return")
    take_all = fin.get("take_all_mean_net_return")
    if mean_net is None or mean_net <= 0:
        reasons.append("selected observations do not have a positive mean net return")
    elif take_all is not None and mean_net <= take_all:
        reasons.append("selected observations do not beat taking every observation")
    wins = walk.get("win_fraction")
    if wins is None or wins < policy.min_walk_forward_win_fraction:
        reasons.append("walk-forward folds do not beat the baseline often enough")
    if reasons:
        return ValidationStatus.FAILED, reasons
    return ValidationStatus.PASSED, [
        "beat the baseline on the untouched test period and in walk-forward validation"
    ]


# --- The run ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    status: RunStatus
    model_version_id: uuid.UUID | None
    validation_status: ValidationStatus | None
    error: str | None


def default_model_name(target: dict[str, Any], model: ModelType, timeframes: Sequence[str]) -> str:
    kind = str(target["kind"]).lower().replace("_", "-")
    tf = "-".join(t.lower() for t in timeframes) or "all"
    family = {
        ModelType.LOGISTIC_REGRESSION: "linear",
        ModelType.RANDOM_FOREST: "rf",
        ModelType.XGBOOST: "xgb",
    }[model]
    return f"{kind}.{tf}.{family}"


class Trainer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: MLSettings,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self.settings = settings
        self.clock = clock

    def train(self, spec: TrainingSpec, *, actor: str) -> RunResult:
        for candidate in (spec.params, *spec.grid):
            parse_params(spec.model_type, candidate)  # reject invalid parameters up front
        with session_scope(self._sessions) as session:
            from app.ml_engine.datasets.store import get_version

            version = get_version(session, spec.dataset_version_id)
            target = dict(version.target_definition)
            run = MLTrainingRunRecord(
                id=uuid.uuid4(),
                dataset_version_id=version.id,
                model_type=spec.model_type,
                task=_task(target),
                status=RunStatus.RUNNING,
                spec=spec.stored(),
                spec_sha256=sha256_json(spec.stored()),
                random_seed=spec.seed,
                target_definition=target,
                code_version={**code_version(), "trainer": TRAINER_VERSION},
                created_by=actor,
                started_at=self.clock.now(),
            )
            session.add(run)
            run_id = run.id
        try:
            return self._execute(run_id, spec, target, actor)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            logger.warning("ml.training_failed", run_id=str(run_id), error_type=type(exc).__name__)
            with session_scope(self._sessions) as session:
                failed = session.get(MLTrainingRunRecord, run_id)
                if failed is not None and failed.status is RunStatus.RUNNING:
                    failed.status = RunStatus.FAILED
                    failed.error = message
                    failed.finished_at = self.clock.now()
            return RunResult(run_id, RunStatus.FAILED, None, None, message)

    def _execute(
        self, run_id: uuid.UUID, spec: TrainingSpec, target: dict[str, Any], actor: str
    ) -> RunResult:
        n_jobs = self.settings.n_jobs
        cost = float(target["round_trip_cost_bps"]) / 10_000
        with session_scope(self._sessions) as session:
            p = prepare(session, spec)
        horizon = int(p.frame["horizon_periods"].cast(pl.Int64).to_numpy().max())
        embargo = spec.split.embargo_periods if spec.split.embargo_periods is not None else horizon
        split = chronological_split(p.line, spec.split, embargo)
        if split.train.size < 30 or split.validation.size < 20 or split.test.size < 1:
            raise TrainingError(
                f"split too small: train {split.train.size}, validation "
                f"{split.validation.size}, test {split.test.size}"
            )
        idx = p.line.index_of()
        test_start_index = int(idx[split.test.mask].min())
        boundary = p.line.times[test_start_index]
        development = (idx < test_start_index) & (p.line.label_end <= boundary)

        # 3. Walk-forward selection on the development period only.
        candidates = [parse_params(spec.model_type, c) for c in (spec.grid or (spec.params,))]
        folds = walk_forward(p.line, development, spec.split.walk_forward_folds, embargo)
        if not folds:
            raise TrainingError("no walk-forward fold could be formed")
        tried = []
        for params in candidates:
            results = run_folds(p, folds, spec, params, cost, n_jobs)
            tried.append((params, results, summarize_folds(p.task, results)))
        ranked = sorted(
            range(len(tried)),
            key=lambda i: (
                tried[i][2]["mean_metric"] if tried[i][2]["mean_metric"] is not None else math.inf,
                i,
            ),
        )
        chosen_params, chosen_results, walk = tried[ranked[0]]
        selection = {
            "candidates": len(candidates),
            "selected_index": ranked[0],
            "criterion": f"lowest mean walk-forward {walk['metric']} (development period only)",
            "scores": [{"params": t[0], "mean_metric": t[2]["mean_metric"]} for t in tried],
            "note": "the test period played no part in selection",
        }

        # 4. Final fit on the training period.
        final = fit(p, split.train.mask, spec, chosen_params, n_jobs)
        frames = {
            name: p.frame.filter(pl.Series(seg.mask))
            for name, seg in (
                ("train", split.train),
                ("validation", split.validation),
                ("test", split.test),
            )
        }
        raw = {name: final.score(frame) for name, frame in frames.items()}

        # 5. Calibration (classification) on validation only.
        decision: CalibrationDecision | None = None
        calibrator = Calibrator(CalibrationMethod.NONE)
        output_kind = OutputKind.REGRESSION_ESTIMATE
        if p.task is TaskKind.CLASSIFICATION:
            order = np.argsort(np.argsort(p.line.decision[split.validation.mask], kind="stable"))
            decision = calibrate(
                raw["validation"],
                p.y[split.validation.mask],
                order.astype(np.int64),
                CalibrationPolicy(),
            )
            if decision.accepted:
                calibrator = decision.calibrator
                output_kind = OutputKind.CALIBRATED_PROBABILITY
            else:
                output_kind = OutputKind.UNCALIBRATED_SCORE
        served = {name: calibrator(score) for name, score in raw.items()}
        # The test period never fits or selects a calibration, but it can withdraw the claim:
        # a calibration that does not hold out of sample is not exposed as a probability.
        calibration_note: str | None = None
        if output_kind is OutputKind.CALIBRATED_PROBABILITY:
            _, test_ece = m.reliability(p.y[split.test.mask], served["test"])
            limit = CalibrationPolicy().max_ece
            if test_ece is None or test_ece > limit:
                output_kind = OutputKind.UNCALIBRATED_SCORE
                calibration_note = (
                    f"Calibration accepted on validation did not hold on the test period "
                    f"(ECE {test_ece if test_ece is not None else float('nan'):.3f} > {limit}): "
                    "outputs are exposed as uncalibrated scores."
                )

        # 6. Evaluation: in-sample (flagged), validation, then the untouched test period.
        top = float(np.quantile(served["validation"], 1 - spec.top_fraction))
        threshold = spec.decision_threshold
        train_metrics, _ = evaluate_block(
            p,
            split.train.mask,
            split.train.mask,
            served["train"],
            threshold=threshold,
            top_threshold=top,
            cost=cost,
        )
        train_metrics["note"] = "in-sample: not evidence of predictive value"
        val_metrics, val_base = evaluate_block(
            p,
            split.train.mask,
            split.validation.mask,
            served["validation"],
            threshold=threshold,
            top_threshold=top,
            cost=cost,
        )
        test_metrics, test_base = evaluate_block(
            p,
            split.train.mask,
            split.test.mask,
            served["test"],
            threshold=threshold,
            top_threshold=top,
            cost=cost,
        )
        test_metrics["top_threshold_from_validation"] = top
        if decision is not None:
            test_metrics["raw_score"] = m.classification_metrics(
                p.y[split.test.mask], raw["test"], threshold
            )
        segments = segment_metrics(
            p, split.train.mask, split.test.mask, served["test"], threshold, cost
        )
        robustness = self._robustness(p, development, spec, chosen_params, embargo, cost, n_jobs)
        status, reasons = evidence_verdict(p.task, test_metrics, test_base, walk, spec.evidence)

        periods = {
            **split.describe(),
            "development_observations": int(development.sum()),
            "rows_used": p.frame.height,
            "rows_rejected": p.rejected,
            "embargo_source": "configured"
            if spec.split.embargo_periods is not None
            else "max label horizon",
        }
        input_schema = self._input_schema(p.data, final.preprocessor, spec)
        with session_scope(self._sessions) as session:
            reuse = self._test_reuse(session, spec.dataset_version_id, periods, run_id)
            run = session.get(MLTrainingRunRecord, run_id)
            if run is None:
                raise RuntimeError("the training run disappeared")
            name = spec.model_name or default_model_name(
                target, spec.model_type, spec.timeframes or tuple(p.data.version.timeframes)
            )
            run.periods = periods
            run.feature_schema = p.data.version.feature_schema
            run.preprocessing = final.preprocessor.describe()
            run.parameters = {
                "model": spec.model_type.value,
                "family": describe_family(spec.model_type, p.task),
                "hyperparameters": chosen_params,
                "seed": spec.seed,
                "n_jobs": n_jobs,
                "sample_weighting": spec.sample_weighting,
                "decision_threshold": threshold,
            }
            run.training_metrics = _clean(train_metrics)
            run.validation_metrics = _clean(val_metrics)
            run.test_metrics = _clean(test_metrics)
            run.baseline_metrics = _clean({"validation": val_base, "test": test_base})
            run.walk_forward = _clean(walk)
            run.robustness = _clean(robustness)
            run.selection = _clean(selection)
            run.validation_status = status
            run.validation_reasons = reasons
            run.test_period_reuse = reuse
            run.notes = [
                *self._notes(p, reuse, output_kind),
                *([calibration_note] if calibration_note else []),
            ]
            self._store_evaluations(
                session,
                run_id,
                p,
                split,
                chosen_results,
                val_metrics,
                val_base,
                test_metrics,
                test_base,
                segments,
                robustness,
            )
            if decision is not None:
                self._store_calibration(
                    session,
                    run_id,
                    p,
                    split,
                    raw,
                    decision,
                    output_kind is OutputKind.CALIBRATED_PROBABILITY,
                )
            session.flush()

            version_number = (
                session.scalar(
                    select(func.max(MLModelVersionRecord.version)).where(
                        MLModelVersionRecord.model_name == name
                    )
                )
                or 0
            ) + 1
            payload = {
                "format": ARTIFACT_FORMAT,
                "model": final.model,
                "preprocessor": final.preprocessor.describe(),
                "calibrator": calibrator,
                "task": p.task.value,
                "output_kind": output_kind.value,
                "input_schema": input_schema,
                "training_run_id": str(run_id),
                "libraries": code_version()["libraries"],
            }
            written = write_artifact(
                self.settings.artifact_root(), f"{name}/v{version_number}-{run_id}.joblib", payload
            )
            artifact = MLModelArtifactRecord(
                training_run_id=run_id,
                path=written.path,
                format=ARTIFACT_FORMAT,
                sha256=written.sha256,
                size_bytes=written.size_bytes,
                libraries=code_version()["libraries"],
            )
            session.add(artifact)
            session.flush()
            model = MLModelVersionRecord(
                model_name=name,
                version=version_number,
                model_type=spec.model_type,
                task=p.task,
                training_run_id=run_id,
                dataset_version_id=spec.dataset_version_id,
                artifact_id=artifact.id,
                input_schema=input_schema,
                input_schema_sha256=sha256_json(input_schema),
                target_definition=target,
                parameters=run.parameters,
                evaluation_summary=_clean(
                    self._summary(
                        p.task, test_metrics, test_base, walk, decision, reuse, output_kind
                    )
                ),
                output_kind=output_kind,
                calibration_method=calibrator.method,
                validation_status=status,
                origin=p.data.version.origin,
                created_by=actor,
            )
            session.add(model)
            session.flush()
            lifecycle.register_initial(session, model, actor)
            run.status = RunStatus.SUCCEEDED
            run.finished_at = self.clock.now()
            model_id = model.id
        logger.info(
            "ml.training_succeeded",
            run_id=str(run_id),
            model=name,
            version=version_number,
            validation_status=status.value,
        )
        return RunResult(run_id, RunStatus.SUCCEEDED, model_id, status, None)

    # --- helpers -------------------------------------------------------------------------

    def _robustness(
        self,
        p: Prepared,
        development: Mask,
        spec: TrainingSpec,
        params: dict[str, Any],
        embargo: int,
        cost: float,
        n_jobs: int,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if spec.split.purged_kfold:
            folds = purged_kfold(p.line, development, spec.split.purged_kfold, embargo)
            out["purged_kfold"] = summarize_folds(
                p.task, run_folds(p, folds, spec, params, cost, n_jobs)
            )
        if spec.split.cpcv_groups:
            folds = cpcv(
                p.line, development, spec.split.cpcv_groups, spec.split.cpcv_test_groups, embargo
            )
            summary = summarize_folds(p.task, run_folds(p, folds, spec, params, cost, n_jobs))
            metrics = [d["metric"] for d in summary["fold_details"] if d["metric"] is not None]
            if metrics:
                summary["metric_quantiles"] = {
                    q: float(np.quantile(metrics, float(q) / 100)) for q in ("10", "50", "90")
                }
            out["cpcv"] = summary
        if out:
            out["note"] = (
                "supplementary robustness on the development period (training data on both "
                "sides of each test block, purged and embargoed); walk-forward remains primary"
            )
        return out

    @staticmethod
    def _input_schema(
        data: DatasetFrame, pre: FittedPreprocessor, spec: TrainingSpec
    ) -> list[dict[str, Any]]:
        used = set(pre.inputs)
        schema = []
        for item in data.version.feature_schema:
            if item["column"] not in used:
                continue
            schema.append(
                {
                    "column": item["column"],
                    "kind": "FEATURE",
                    "keys": item["keys"],
                    "value_kind": item["value_kind"],
                    "definition_sha256": item["definition_sha256"],
                    "missing_allowed": bool(item.get("optional")),
                }
            )
        for column in spec.preprocessing.context:
            if column in pre.categorical:
                schema.append(
                    {
                        "column": column,
                        "kind": "CONTEXT",
                        "value_kind": "LABEL",
                        "categories": pre.categorical[column],
                        "missing_allowed": False,
                    }
                )
        return schema

    @staticmethod
    def _test_reuse(
        session: Session, dataset_version_id: uuid.UUID, periods: dict[str, Any], run_id: uuid.UUID
    ) -> int:
        r = MLTrainingRunRecord
        rows = session.execute(
            select(r.id, r.periods).where(
                r.dataset_version_id == dataset_version_id,
                r.status == RunStatus.SUCCEEDED,
                r.id != run_id,
            )
        ).all()
        test = periods["test"]
        return sum(
            1
            for row in rows
            if row.periods
            and row.periods.get("test", {}).get("start") == test["start"]
            and row.periods.get("test", {}).get("end") == test["end"]
        )

    @staticmethod
    def _notes(p: Prepared, reuse: int, output_kind: OutputKind) -> list[str]:
        notes = list(p.data.version.warnings)
        if reuse:
            notes.append(
                f"The test period was already evaluated by {reuse} earlier run(s) on this "
                "dataset version: comparing many runs on it erodes its independence."
            )
        if output_kind is OutputKind.UNCALIBRATED_SCORE:
            notes.append("Outputs are uncalibrated scores, not probabilities.")
        return notes

    @staticmethod
    def _summary(
        task: TaskKind,
        test: dict[str, Any],
        base: dict[str, Any],
        walk: dict[str, Any],
        decision: CalibrationDecision | None,
        reuse: int,
        output_kind: OutputKind,
    ) -> dict[str, Any]:
        keys = (
            (
                "roc_auc",
                "average_precision",
                "log_loss",
                "brier",
                "ece",
                "precision",
                "recall",
                "f1",
            )
            if task is TaskKind.CLASSIFICATION
            else ("mae", "rmse", "r2", "directional_accuracy", "rank_correlation")
        )
        return {
            "test": {k: test.get(k) for k in ("observations", *keys)},
            "test_baseline": {k: base.get(k) for k in keys},
            "test_financial": test.get("financial"),
            "walk_forward": {
                k: walk.get(k)
                for k in ("folds", "mean_metric", "mean_baseline_metric", "win_fraction")
            },
            "calibration": None
            if decision is None
            else {
                "method": decision.selected.value,
                "accepted_on_validation": decision.accepted,
                "exposed_as_probability": output_kind is OutputKind.CALIBRATED_PROBABILITY,
                "reason": decision.reason,
            },
            "test_period_reuse": reuse,
        }

    @staticmethod
    def _store_evaluations(
        session: Session,
        run_id: uuid.UUID,
        p: Prepared,
        split: Split,
        folds: Sequence[FoldResult],
        val: dict[str, Any],
        val_base: dict[str, Any],
        test: dict[str, Any],
        test_base: dict[str, Any],
        segments: dict[SegmentKind, dict[str, dict[str, Any]]],
        robustness: dict[str, Any],
    ) -> None:
        def add(
            kind: SplitKind,
            fold: int | None,
            seg: SegmentKind,
            value: str,
            n: int,
            metrics: dict[str, Any],
            baseline: dict[str, Any],
            start: datetime | None = None,
            end: datetime | None = None,
        ) -> None:
            session.add(
                MLModelEvaluationRecord(
                    training_run_id=run_id,
                    split=kind,
                    fold=fold,
                    segment_kind=seg,
                    segment_value=value[:64],
                    period_start=start,
                    period_end=end,
                    observations=n,
                    metrics=_clean(metrics),
                    baseline_metrics=_clean(baseline),
                )
            )

        for r in folds:
            start, end = p.line.when(r.fold.test)
            add(
                SplitKind.WALK_FORWARD,
                r.fold.index,
                SegmentKind.ALL,
                "ALL",
                int(r.fold.test.sum()),
                r.metrics,
                r.baseline,
                start,
                end,
            )
        add(
            SplitKind.VALIDATION,
            None,
            SegmentKind.ALL,
            "ALL",
            split.validation.size,
            val,
            val_base,
            split.validation.start,
            split.validation.end,
        )
        add(
            SplitKind.TEST,
            None,
            SegmentKind.ALL,
            "ALL",
            split.test.size,
            test,
            test_base,
            split.test.start,
            split.test.end,
        )
        for kind, table in segments.items():
            for value, result in table.items():
                add(
                    SplitKind.TEST,
                    None,
                    kind,
                    value,
                    int(result.get("model", result).get("observations", 0)),
                    result.get("model", {"insufficient": True, **result}),
                    result.get("baseline", {}),
                    split.test.start,
                    split.test.end,
                )
        for key, split_kind in (("purged_kfold", SplitKind.PURGED_KFOLD), ("cpcv", SplitKind.CPCV)):
            summary = robustness.get(key)
            if summary:
                add(split_kind, None, SegmentKind.ALL, "ALL", int(split.train.size), summary, {})

    @staticmethod
    def _store_calibration(
        session: Session,
        run_id: uuid.UUID,
        p: Prepared,
        split: Split,
        raw: dict[str, Floats],
        decision: CalibrationDecision,
        exposed: bool,
    ) -> None:
        for evidence in decision.evidence:
            session.add(
                MLCalibrationResultRecord(
                    training_run_id=run_id,
                    method=evidence.method,
                    fitted_on=SplitKind.VALIDATION,
                    evaluated_on="VALIDATION_HOLDOUT",
                    observations=evidence.observations,
                    brier_raw=decision.holdout_raw.brier,
                    brier_calibrated=evidence.brier,
                    brier_base_rate=decision.base_rate_brier,
                    ece_raw=decision.holdout_raw.ece,
                    ece_calibrated=evidence.ece,
                    reliability_raw=_clean(decision.holdout_raw.reliability),
                    reliability_calibrated=_clean(evidence.reliability),
                    selected=evidence.method is decision.selected,
                    accepted=decision.accepted and evidence.method is decision.selected,
                    decision=decision.reason,
                )
            )
        y = p.y[split.test.mask]
        raw_table, raw_ece = m.reliability(y, raw["test"])
        fitted = decision.calibrator(raw["test"])
        cal_table, cal_ece = m.reliability(y, fitted)
        base = float(p.y[split.validation.mask].mean())
        session.add(
            MLCalibrationResultRecord(
                training_run_id=run_id,
                method=decision.selected,
                fitted_on=SplitKind.VALIDATION,
                evaluated_on="TEST",
                observations=int(y.size),
                brier_raw=m.brier(y, raw["test"]),
                brier_calibrated=m.brier(y, fitted),
                brier_base_rate=m.brier(y, np.full(y.size, base)),
                ece_raw=raw_ece,
                ece_calibrated=cal_ece,
                reliability_raw=_clean(raw_table),
                reliability_calibrated=_clean(cal_table),
                selected=True,
                accepted=exposed,
                decision="out-of-sample report on the untouched test period (never used to fit "
                "or select; it can only withdraw the probability claim); "
                + ("exposed as a probability" if exposed else "not exposed as a probability")
                + "; validation: "
                + decision.reason,
            )
        )


def _clean(value: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, non-finite floats to None."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def statuses_for(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, LifecycleStatus]:
    return lifecycle.statuses(session, ids)


Score = Callable[[pl.DataFrame], Floats]

__all__ = [
    "MAX_GRID",
    "TRAINER_VERSION",
    "EvidencePolicy",
    "RunResult",
    "Trainer",
    "TrainingError",
    "TrainingSpec",
    "default_model_name",
    "evidence_verdict",
    "prepare",
]

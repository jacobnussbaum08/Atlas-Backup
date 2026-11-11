"""Candidate ML model comparison under the same chronological discipline (Phase 8B).

The 5A trainer already fits logistic regression (ridge for regression targets), random
forest and XGBoost with chronological walk-forward selection, purging and embargo,
calibration on validation data only, and one evaluation of an untouched test period. This
module compares those runs **against each other, against simple baselines and against an
approved model**, keeping two questions apart:

1. **Predictive performance** on the test period (log loss / Brier against the constant
   base-rate model; R^2 and rank correlation for regression), walk-forward folds beating
   the baseline, and calibration (validation -> test ECE change).
2. **Financial usefulness**: the model's trade/no-trade policy on the test period, net of
   the dataset's stated round-trip cost, against *take every observation* and *take none*
   (zero). Accuracy is never used.

**Incremental value** (the only route to a SHADOW model challenger) needs all of:

* better test log loss / Brier than the base rate (classification) or R^2 > 0 (regression);
* the policy's mean net return per observation is positive and above take-all;
* it stays positive at **twice** the assumed costs;
* it is significant after the number of runs tried on that dataset (Bonferroni over every
  hyperparameter candidate of every run), with the sample size divided by the dataset's
  mean label concurrency because overlapping labels are not independent;
* at least :data:`MIN_EFFECTIVE` effective observations.

Never promote a model because a classification score improved.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from typing import Any

from scipy import stats as sps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.config import sha256_of
from app.database.models.learning import ValidationComparisonRecord
from app.database.models.ml import (
    MLDatasetVersionRecord,
    MLModelVersionRecord,
    MLTrainingRunRecord,
)
from app.learning.enums import SubjectKind
from app.ml_engine.enums import LifecycleStatus, RunStatus, TaskKind
from app.ml_engine.registry import lifecycle as ml

MIN_EFFECTIVE = 100
ALPHA = 0.05


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


LOWER_IS_BETTER = frozenset({"log_loss", "brier", "rmse", "mae", "ece"})
HIGHER_IS_BETTER = frozenset({"roc_auc", "r2", "rank_correlation", "average_precision"})


def _fold_wins(walk_forward: dict[str, Any] | None) -> dict[str, Any]:
    """Walk-forward folds where the model beat its baseline, in the metric's own direction."""
    metric = (walk_forward or {}).get("metric")
    folds = (walk_forward or {}).get("fold_details") or []
    if metric not in LOWER_IS_BETTER | HIGHER_IS_BETTER:
        return {"folds": 0, "beating_baseline": 0, "metric": metric, "note": "unknown direction"}
    lower = metric in LOWER_IS_BETTER
    wins = 0
    scored = 0
    for f in folds:
        m, b = _f(f.get("metric")), _f(f.get("baseline_metric"))
        if m is None or b is None:
            continue
        scored += 1
        wins += int(m < b if lower else m > b)
    return {"folds": scored, "beating_baseline": wins, "metric": metric}


def assess_run(
    run: MLTrainingRunRecord, *, concurrency: float, tests: int, approved: bool
) -> dict[str, Any]:
    test = run.test_metrics or {}
    base = (run.baseline_metrics or {}).get("test", {})
    validation = run.validation_metrics or {}
    classification = run.task is TaskKind.CLASSIFICATION
    reasons: list[str] = []
    predictive: dict[str, Any]
    if classification:
        ll, bll = _f(test.get("log_loss")), _f(base.get("log_loss"))
        br, bbr = _f(test.get("brier")), _f(base.get("brier"))
        better = (
            ll is not None
            and bll is not None
            and ll < bll
            and br is not None
            and bbr is not None
            and br < bbr
        )
        predictive = {
            "log_loss": ll,
            "baseline_log_loss": bll,
            "brier": br,
            "baseline_brier": bbr,
            "roc_auc": _f(test.get("roc_auc")),
            "beats_base_rate": better,
        }
        if not better:
            reasons.append("test log loss / Brier not better than the constant base rate")
    else:
        r2 = _f(test.get("r2"))
        better = r2 is not None and r2 > 0
        predictive = {
            "r2": r2,
            "rmse": _f(test.get("rmse")),
            "rank_correlation": _f(test.get("rank_correlation")),
            "beats_mean_prediction": better,
        }
        if not better:
            reasons.append("test R^2 is not above zero (no better than the training mean)")
    calibration = {
        "validation_ece": _f(validation.get("ece")),
        "test_ece": _f(test.get("ece")),
        "raw_test_ece": _f((test.get("raw_score") or {}).get("ece")),
    }
    if calibration["validation_ece"] is not None and calibration["test_ece"] is not None:
        calibration["ece_change"] = calibration["test_ece"] - calibration["validation_ece"]
    fin = test.get("financial") or {}
    take_all = (base.get("financial_take_all") or {}) if classification else {}
    mean = _f(fin.get("mean_net_return"))
    sd = _f(fin.get("std_net_return"))
    trades = int(fin.get("trades") or 0)
    rtc = _f(fin.get("round_trip_cost")) or 0.0
    take_all_mean = _f(take_all.get("mean_net_return")) or _f(fin.get("take_all_mean_net_return"))
    effective = trades / max(concurrency, 1.0)
    p_value = None
    if mean is not None and sd and effective >= 2:
        t = mean / (sd / math.sqrt(effective))
        p_value = float(sps.t.sf(t, max(effective - 1, 1)))
    p_adjusted = None if p_value is None else min(1.0, p_value * max(tests, 1))
    doubled = None if mean is None else mean - rtc
    financial = {
        "policy_trades": trades,
        "effective_observations": effective,
        "mean_label_concurrency": concurrency,
        "mean_net_return": mean,
        "take_all_mean_net_return": take_all_mean,
        "take_none_mean_net_return": 0.0,
        "edge_over_take_all": None
        if mean is None or take_all_mean is None
        else mean - take_all_mean,
        "mean_net_return_at_2x_costs": doubled,
        "round_trip_cost": rtc,
        "profit_factor": _f(fin.get("profit_factor")),
        "hit_rate": _f(fin.get("hit_rate")),
        "p_value": p_value,
        "p_value_bonferroni": p_adjusted,
        "tests_on_dataset": tests,
        "note": fin.get("note"),
    }
    if mean is None or mean <= 0:
        reasons.append("the model's policy loses money after costs (worse than taking no trade)")
    if take_all_mean is not None and mean is not None and mean <= take_all_mean:
        reasons.append("the policy does not beat taking every observation")
    if doubled is not None and doubled <= 0:
        reasons.append("the policy is not profitable at twice the assumed costs")
    if effective < MIN_EFFECTIVE:
        reasons.append(f"{effective:.0f} effective observations (< {MIN_EFFECTIVE})")
    if p_adjusted is None or p_adjusted >= ALPHA:
        reasons.append(
            "not significant after "
            f"{tests} tests on this dataset (adjusted p "
            f"{'n/a' if p_adjusted is None else round(p_adjusted, 4)})"
        )
    return {
        "training_run_id": str(run.id),
        "model_type": run.model_type.value,
        "task": run.task.value,
        "validation_status_5a": None
        if run.validation_status is None
        else run.validation_status.value,
        "test_period_reuse": run.test_period_reuse,
        "test_period": (run.periods or {}).get("test"),
        "approved_champion": approved,
        "predictive": predictive,
        "walk_forward": _fold_wins(run.walk_forward),
        "calibration": calibration,
        "financial": financial,
        "incremental_value": not reasons,
        "reasons": reasons,
    }


def compare(session: Session, dataset_version_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """One comparison per dataset version (runs on the same data and test period)."""
    q = select(MLTrainingRunRecord).where(MLTrainingRunRecord.status == RunStatus.SUCCEEDED)
    if dataset_version_id is not None:
        q = q.where(MLTrainingRunRecord.dataset_version_id == dataset_version_id)
    runs = list(session.scalars(q.order_by(MLTrainingRunRecord.started_at)))
    groups: dict[uuid.UUID, list[MLTrainingRunRecord]] = defaultdict(list)
    for r in runs:
        groups[r.dataset_version_id].append(r)
    approved_runs: set[uuid.UUID] = set()
    for model in session.scalars(select(MLModelVersionRecord)):
        if ml.current_status(session, model.id) is LifecycleStatus.APPROVED:
            approved_runs.add(model.training_run_id)
    out: list[dict[str, Any]] = []
    for dv_id, members in groups.items():
        dv = session.get(MLDatasetVersionRecord, dv_id)
        stats = {} if dv is None else (dv.statistics or {})
        concurrency = _f(stats.get("mean_label_concurrency")) or 1.0
        tests = sum(int((m.selection or {}).get("candidates") or 1) for m in members)
        rows = [
            assess_run(m, concurrency=concurrency, tests=tests, approved=m.id in approved_runs)
            for m in members
        ]
        out.append(
            {
                "dataset_version_id": str(dv_id),
                "observations": stats.get("observations"),
                "mean_label_concurrency": concurrency,
                "runs": len(members),
                "hyperparameter_candidates_tried": tests,
                "models": rows,
                "any_incremental_value": any(r["incremental_value"] for r in rows),
                "baselines": "constant training base rate (classification) or training mean "
                "(regression); take every observation; take none",
                "statement": "test-period figures of 5A runs; financial outcomes are per "
                "observation, overlap in time and are not sized: no portfolio return is implied",
            }
        )
    return out


def store_comparison(
    session: Session, *, actor: str, dataset_version_id: uuid.UUID | None = None
) -> uuid.UUID:
    groups = compare(session, dataset_version_id)
    result = {"groups": groups, "models": [m for g in groups for m in g["models"]]}
    row = ValidationComparisonRecord(
        kind=SubjectKind.MODEL,
        title="ML candidate comparison"
        + ("" if dataset_version_id is None else f" ({dataset_version_id})"),
        subjects={
            "dataset_version_id": None if dataset_version_id is None else str(dataset_version_id)
        },
        result=result,
        result_sha256=sha256_of(result),
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row.id


__all__ = ["ALPHA", "MIN_EFFECTIVE", "assess_run", "compare", "store_comparison"]

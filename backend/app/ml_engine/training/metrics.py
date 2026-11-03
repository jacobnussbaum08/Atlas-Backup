"""Evaluation metrics (Phase 5A).

* Classification: precision, recall, F1, ROC-AUC (only when both classes occur), average
  precision (PR-AUC), log loss, Brier score, expected calibration error, confusion matrix.
  Accuracy alone is never reported as a verdict.
* Regression: MAE, RMSE, R^2, rank correlation, directional accuracy (sign agreement on
  non-zero outcomes) and the error distribution.
* Financial: the realized net outcome (after the stated cost assumption) of the
  observations a model would select, against taking every observation and taking none.
  Per-trade statistics only: observations overlap in time and share capital limits that
  do not exist yet, so no annualized return or Sharpe ratio is claimed.

Scores passed to the classification metrics are whatever the model outputs; whether they
may be called probabilities is decided by calibration (``calibration.py``), not here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

MIN_SEGMENT = 30  # segments with fewer observations are reported as insufficient
EPS = 1e-12

Floats = NDArray[np.float64]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def reliability(y: Floats, p: Floats, bins: int = 10) -> tuple[list[dict[str, Any]], float | None]:
    """Uniform-width reliability bins and the expected calibration error."""
    if y.size == 0:
        return [], None
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(p, edges[1:-1], right=False), 0, bins - 1)
    table: list[dict[str, Any]] = []
    ece = 0.0
    for b in range(bins):
        mask = index == b
        count = int(mask.sum())
        row: dict[str, Any] = {
            "lower": float(edges[b]),
            "upper": float(edges[b + 1]),
            "count": count,
            "mean_score": None,
            "observed_rate": None,
        }
        if count:
            mean_p, rate = float(p[mask].mean()), float(y[mask].mean())
            row.update({"mean_score": mean_p, "observed_rate": rate})
            ece += count / y.size * abs(rate - mean_p)
        table.append(row)
    return table, ece


def brier(y: Floats, p: Floats) -> float | None:
    return _f(np.mean((p - y) ** 2)) if y.size else None


def log_loss(y: Floats, p: Floats) -> float | None:
    if y.size == 0:
        return None
    q = np.clip(p, EPS, 1 - EPS)
    return _f(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def classification_metrics(y: Floats, score: Floats, threshold: float = 0.5) -> dict[str, Any]:
    from sklearn.metrics import average_precision_score, roc_auc_score

    n = int(y.size)
    if n == 0:
        return {"observations": 0}
    predicted = score >= threshold
    positive = y == 1
    tp = int((predicted & positive).sum())
    fp = int((predicted & ~positive).sum())
    fn = int((~predicted & positive).sum())
    tn = int((~predicted & ~positive).sum())
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    both = 0 < positive.sum() < n
    _, ece = reliability(y, score)
    return {
        "observations": n,
        "base_rate": _f(positive.mean()),
        "threshold": threshold,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "roc_auc": _f(roc_auc_score(y, score)) if both else None,
        "average_precision": _f(average_precision_score(y, score)) if both else None,
        "log_loss": log_loss(y, score),
        "brier": brier(y, score),
        "ece": ece,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "predicted_positive_rate": _f(predicted.mean()),
        "mean_score": _f(score.mean()),
        "accuracy_note": "accuracy is not used to judge models",
    }


def regression_metrics(y: Floats, pred: Floats) -> dict[str, Any]:
    from scipy.stats import spearmanr

    n = int(y.size)
    if n == 0:
        return {"observations": 0}
    err = pred - y
    nonzero = y != 0
    directional = (
        float((np.sign(pred[nonzero]) == np.sign(y[nonzero])).mean()) if nonzero.any() else None
    )
    ss_tot = float(((y - y.mean()) ** 2).sum())
    rank = spearmanr(pred, y).statistic if n > 2 and np.std(pred) > 0 else None
    return {
        "observations": n,
        "mae": _f(np.abs(err).mean()),
        "rmse": _f(np.sqrt((err**2).mean())),
        "r2": _f(1 - float((err**2).sum()) / ss_tot) if ss_tot > 0 else None,
        "rank_correlation": _f(rank),
        "directional_accuracy": directional,
        "error_quantiles": {
            q: _f(np.quantile(err, float(q) / 100)) for q in ("5", "25", "50", "75", "95")
        },
        "mean_prediction": _f(pred.mean()),
        "mean_outcome": _f(y.mean()),
    }


def financial_metrics(net: Floats, take: NDArray[np.bool_], cost: float) -> dict[str, Any]:
    """Outcomes of the observations ``take`` selects (net of the cost assumption)."""
    n = int(net.size)
    chosen = net[take]
    trades = int(chosen.size)
    result: dict[str, Any] = {
        "candidates": n,
        "trades": trades,
        "coverage": trades / n if n else None,
        "round_trip_cost": cost,
        "costs_paid": trades * cost,
        "take_all_mean_net_return": _f(net.mean()) if n else None,
        "take_all_hit_rate": _f((net > 0).mean()) if n else None,
    }
    if trades == 0:
        result.update({"mean_net_return": None, "note": "the model selected no observation"})
        return result
    wins, losses = chosen[chosen > 0], chosen[chosen <= 0]
    sd = float(chosen.std(ddof=1)) if trades > 1 else None
    result.update(
        {
            "mean_net_return": _f(chosen.mean()),  # expected value per selected observation
            "median_net_return": _f(np.median(chosen)),
            "sum_net_return": _f(chosen.sum()),
            "hit_rate": _f((chosen > 0).mean()),
            "average_win": _f(wins.mean()) if wins.size else None,
            "average_loss": _f(losses.mean()) if losses.size else None,
            "profit_factor": _f(wins.sum() / -losses.sum())
            if losses.size and losses.sum() < 0
            else None,
            "std_net_return": _f(sd),
            "mean_over_std": _f(chosen.mean() / sd) if sd else None,
            "edge_over_take_all": _f(chosen.mean() - net.mean()),
            "note": "per-observation outcomes; observations overlap in time and are not "
            "sized, so no portfolio return or annualized ratio is implied",
        }
    )
    return result


def segment_table(
    values: Sequence[str], mask_fn: Any, min_size: int = MIN_SEGMENT
) -> dict[str, dict[str, Any]]:
    """Apply ``mask_fn(mask) -> metrics`` to each distinct value of ``values``."""
    array = np.asarray(values, dtype=object)
    out: dict[str, dict[str, Any]] = {}
    for value in sorted({str(v) for v in values}):
        mask = array == value
        size = int(mask.sum())
        if size < min_size:
            out[value] = {"observations": size, "insufficient": True}
        else:
            out[value] = {**mask_fn(mask), "insufficient": False}
    return out


__all__ = [
    "MIN_SEGMENT",
    "brier",
    "classification_metrics",
    "financial_metrics",
    "log_loss",
    "regression_metrics",
    "reliability",
    "segment_table",
]

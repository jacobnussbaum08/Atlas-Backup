"""Probability calibration fitted on validation data only (Phase 5A).

A classifier's score is not a probability until evidence says so. The procedure:

1. The validation period is split chronologically in two halves. Platt scaling (SIGMOID)
   and ISOTONIC regression are each fitted on the first half and measured on the second
   (out of sample), next to the raw score (NONE) and the first half's base rate.
2. The method with the lowest held-out Brier score is selected. Its output may be exposed
   as a **probability** only if, on the held-out half, there are at least ``min_holdout``
   observations, its expected calibration error is at most ``max_ece`` and its Brier
   score is no worse than the base rate's. Otherwise the model's output is labelled an
   UNCALIBRATED_SCORE.
3. The selected method is refitted on the whole validation period. The untouched test
   period is used only to *report* calibration (raw and calibrated), never to fit or select.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.ml_engine.enums import CalibrationMethod
from app.ml_engine.training.metrics import brier, reliability

Floats = NDArray[np.float64]
EPS = 1e-6


@dataclass(frozen=True)
class CalibrationPolicy:
    min_holdout: int = 100
    max_ece: float = 0.05


class Calibrator:
    """A fitted monotone map from raw score to calibrated score."""

    def __init__(self, method: CalibrationMethod, model: Any = None) -> None:
        self.method = method
        self.model = model

    def __call__(self, score: Floats) -> Floats:
        if self.method is CalibrationMethod.NONE:
            return score
        if self.method is CalibrationMethod.SIGMOID:
            return np.asarray(self.model.predict_proba(_logit(score))[:, 1], dtype=np.float64)
        return np.asarray(self.model.predict(score), dtype=np.float64)


def _logit(score: Floats) -> Floats:
    p = np.clip(score, EPS, 1 - EPS)
    return np.log(p / (1 - p)).reshape(-1, 1)


def fit_calibrator(method: CalibrationMethod, score: Floats, y: Floats) -> Calibrator:
    if method is CalibrationMethod.NONE:
        return Calibrator(method)
    if method is CalibrationMethod.SIGMOID:
        from sklearn.linear_model import LogisticRegression

        model = LogisticRegression(C=1e6, max_iter=1000)
        model.fit(_logit(score), y.astype(int))
        return Calibrator(method, model)
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(score, y)
    return Calibrator(method, iso)


@dataclass(frozen=True)
class MethodEvidence:
    method: CalibrationMethod
    observations: int
    brier: float | None
    ece: float | None
    reliability: list[dict[str, Any]]


@dataclass(frozen=True)
class CalibrationDecision:
    selected: CalibrationMethod
    accepted: bool
    reason: str
    base_rate_brier: float | None
    holdout_raw: MethodEvidence
    evidence: list[MethodEvidence]
    calibrator: Calibrator  # the selected method refitted on all validation data


def _evidence(method: CalibrationMethod, y: Floats, p: Floats) -> MethodEvidence:
    table, ece = reliability(y, p)
    return MethodEvidence(method, int(y.size), brier(y, p), ece, table)


def calibrate(
    score: Floats, y: Floats, order: NDArray[np.int64], policy: CalibrationPolicy
) -> CalibrationDecision:
    """``score``/``y``: validation scores and labels; ``order``: their chronological order."""
    ordered = np.argsort(order, kind="stable")
    half = ordered.size // 2
    first, second = ordered[:half], ordered[half:]
    raw = _evidence(CalibrationMethod.NONE, y[second], score[second])
    base = float(y[first].mean()) if first.size else 0.5
    base_brier = brier(y[second], np.full(second.size, base))
    evidence = [raw]
    fittable = first.size >= 20 and 0 < y[first].sum() < first.size
    if fittable:
        for method in (CalibrationMethod.SIGMOID, CalibrationMethod.ISOTONIC):
            fitted = fit_calibrator(method, score[first], y[first])
            evidence.append(_evidence(method, y[second], fitted(score[second])))
    candidates = [
        e for e in evidence if e.method is not CalibrationMethod.NONE and e.brier is not None
    ]
    if not candidates:
        return CalibrationDecision(
            CalibrationMethod.NONE,
            False,
            "too little validation data (or a single class) to fit a calibration",
            base_brier,
            raw,
            evidence,
            Calibrator(CalibrationMethod.NONE),
        )
    best = min(candidates, key=lambda e: (float(e.brier or 0.0), e.method.value))
    problems = []
    if best.observations < policy.min_holdout:
        problems.append(f"only {best.observations} held-out observations (< {policy.min_holdout})")
    if best.ece is None or best.ece > policy.max_ece:
        problems.append(f"held-out ECE {best.ece:.3f} > {policy.max_ece}" if best.ece else "no ECE")
    if base_brier is not None and best.brier is not None and best.brier > base_brier:
        problems.append(
            f"held-out Brier {best.brier:.4f} worse than the base rate's {base_brier:.4f}"
        )
    accepted = not problems
    reason = (
        f"{best.method.value} accepted: held-out ECE {best.ece:.3f}, Brier {best.brier:.4f} "
        f"(base rate {base_brier:.4f})"
        if accepted and best.ece is not None and best.brier is not None and base_brier is not None
        else "not accepted as a probability: " + "; ".join(problems)
    )
    return CalibrationDecision(
        best.method,
        accepted,
        reason,
        base_brier,
        raw,
        evidence,
        fit_calibrator(best.method, score, y),
    )


__all__ = [
    "CalibrationDecision",
    "CalibrationPolicy",
    "Calibrator",
    "MethodEvidence",
    "calibrate",
    "fit_calibrator",
]

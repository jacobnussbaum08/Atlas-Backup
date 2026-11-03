"""Phase 5A metrics, calibration, evidence verdicts and model factories (pure).

Scores and outcomes here are synthetic TEST data generated with a fixed seed.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.ml_engine.enums import CalibrationMethod, ModelType, TaskKind, ValidationStatus
from app.ml_engine.training import metrics as m
from app.ml_engine.training.calibration import CalibrationPolicy, calibrate
from app.ml_engine.training.models import build_model, parse_params
from app.ml_engine.training.trainer import EvidencePolicy, evidence_verdict


def test_classification_metrics_are_not_accuracy():
    y = np.array([1, 0, 1, 1, 0, 0], dtype=float)
    s = np.array([0.9, 0.2, 0.6, 0.4, 0.7, 0.1])
    out = m.classification_metrics(y, s)
    assert out["confusion"] == {"tp": 2, "fp": 1, "tn": 2, "fn": 1}
    assert out["precision"] == pytest.approx(2 / 3)
    assert out["recall"] == pytest.approx(2 / 3)
    assert out["roc_auc"] == pytest.approx(7 / 9)
    assert "accuracy" not in out


def test_auc_is_absent_with_a_single_class():
    out = m.classification_metrics(np.ones(4), np.array([0.1, 0.2, 0.3, 0.4]))
    assert out["roc_auc"] is None


def test_regression_metrics():
    y = np.array([0.01, -0.02, 0.03, 0.0])
    p = np.array([0.02, -0.01, -0.01, 0.01])
    out = m.regression_metrics(y, p)
    assert out["mae"] == pytest.approx(np.abs(p - y).mean())
    assert out["directional_accuracy"] == pytest.approx(2 / 3)  # zero outcomes excluded


def test_financial_metrics_compare_with_taking_everything():
    net = np.array([0.02, -0.01, 0.03, -0.02])
    out = m.financial_metrics(net, np.array([True, False, True, False]), cost=0.001)
    assert out["trades"] == 2
    assert out["mean_net_return"] == pytest.approx(0.025)
    assert out["take_all_mean_net_return"] == pytest.approx(0.005)
    assert out["costs_paid"] == pytest.approx(0.002)
    none = m.financial_metrics(net, np.zeros(4, bool), cost=0.001)
    assert none["mean_net_return"] is None


def test_reliability_and_ece():
    y = np.array([0, 0, 1, 1], dtype=float)
    p = np.array([0.05, 0.05, 0.95, 0.95])
    table, ece = m.reliability(y, p)
    assert ece == pytest.approx(0.05)
    assert sum(b["count"] for b in table) == 4


def test_calibration_is_accepted_only_with_evidence():
    rng = np.random.default_rng(1)
    true_p = rng.uniform(0.2, 0.8, 4000)
    y = (rng.uniform(size=4000) < true_p).astype(float)
    distorted = true_p**3  # monotone, badly calibrated score
    order = np.arange(4000, dtype=np.int64)
    good = calibrate(distorted, y, order, CalibrationPolicy())
    assert good.selected in (CalibrationMethod.SIGMOID, CalibrationMethod.ISOTONIC)
    assert good.accepted, good.reason
    assert good.holdout_raw.ece is not None
    assert good.holdout_raw.ece > 0.1  # the raw score was not a probability
    small = calibrate(distorted[:80], y[:80], order[:80], CalibrationPolicy())
    assert not small.accepted
    noise = rng.uniform(size=4000)
    useless = calibrate(
        noise, (rng.uniform(size=4000) < 0.5).astype(float), order, CalibrationPolicy()
    )
    assert useless.calibrator(np.array([0.5]))[0] == pytest.approx(0.5, abs=0.1)


def test_evidence_verdict():
    policy = EvidencePolicy(min_test_observations=100)
    walk = {"win_fraction": 0.75}
    good = {
        "observations": 500,
        "log_loss": 0.68,
        "roc_auc": 0.56,
        "financial": {"mean_net_return": 0.004, "take_all_mean_net_return": 0.001},
    }
    base = {"log_loss": 0.69}
    assert (
        evidence_verdict(TaskKind.CLASSIFICATION, good, base, walk, policy)[0]
        is ValidationStatus.PASSED
    )
    worse = {**good, "log_loss": 0.70}
    status, reasons = evidence_verdict(TaskKind.CLASSIFICATION, worse, base, walk, policy)
    assert status is ValidationStatus.FAILED
    assert any("baseline" in r for r in reasons)
    few = {**good, "observations": 50}
    assert evidence_verdict(TaskKind.CLASSIFICATION, few, base, walk, policy)[0] is (
        ValidationStatus.INSUFFICIENT_EVIDENCE
    )
    unprofitable = {**good, "financial": {"mean_net_return": -0.001}}
    assert evidence_verdict(TaskKind.CLASSIFICATION, unprofitable, base, walk, policy)[0] is (
        ValidationStatus.FAILED
    )


@pytest.mark.parametrize("model", list(ModelType))
@pytest.mark.parametrize("task", list(TaskKind))
def test_models_are_seeded_and_reproducible(model, task):
    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, 4))
    y = (
        (x[:, 0] + rng.normal(size=300) > 0).astype(float)
        if task is TaskKind.CLASSIFICATION
        else x[:, 0]
    )
    params = parse_params(
        model, {"n_estimators": 20} if model is not ModelType.LOGISTIC_REGRESSION else {}
    )
    outputs = []
    for _ in range(2):
        est = build_model(model, task, params, seed=7, n_jobs=1)
        est.fit(x, y)
        out = est.predict_proba(x)[:, 1] if task is TaskKind.CLASSIFICATION else est.predict(x)
        outputs.append(out)
    assert np.array_equal(outputs[0], outputs[1])


def test_invalid_hyperparameters_are_refused():
    with pytest.raises(ValueError, match="max_depth"):
        parse_params(ModelType.XGBOOST, {"max_depth": 0})
    with pytest.raises(ValueError, match="unknown"):
        parse_params(ModelType.RANDOM_FOREST, {"unknown": 1})

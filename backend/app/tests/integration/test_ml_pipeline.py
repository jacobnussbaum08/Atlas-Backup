"""Phase 5A end to end on a real PostgreSQL: dataset -> training -> registry -> inference.

All data is TEST FIXTURE material (``ml_support``): synthetic bars, origin TEST_FIXTURE.
No assertion here says anything about real markets.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.database.models import (
    MLCalibrationResultRecord,
    MLDatasetObservationRecord,
    MLDatasetVersionRecord,
    MLModelEvaluationRecord,
    MLModelVersionRecord,
    MLPredictionRecord,
    MLTrainingRunRecord,
)
from app.database.sessions import session_scope
from app.ml_engine.datasets.store import load_frame, verify_dataset
from app.ml_engine.enums import (
    DataOrigin,
    LabelStatus,
    LifecycleStatus,
    ModelType,
    OutputKind,
    PredictionMode,
    PredictionValidity,
    RunStatus,
    SplitKind,
)
from app.ml_engine.inference.engine import PredictionRequest
from app.ml_engine.registry import lifecycle
from app.ml_engine.training.trainer import TrainingSpec
from app.strategies.enums import Direction
from app.tests.integration.ml_support import (
    CAL,
    build_and_save,
    fetch,
    make_ml_lab,
    seed_universe,
    spec,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def lab(fresh_engine, tmp_path):
    return make_ml_lab(fresh_engine, tmp_path / "artifacts")


def _train(lab, version_id, model=ModelType.LOGISTIC_REGRESSION, **kw):
    return lab.trainer.train(
        TrainingSpec(dataset_version_id=version_id, model_type=model, **kw), actor="test"
    )


def test_dataset_is_point_in_time_labelled_and_reproducible(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    with session_scope(lab.factory) as session:
        version = fetch(session, MLDatasetVersionRecord, version_id)
        assert version.origin is DataOrigin.TEST_FIXTURE
        assert any("TEST FIXTURE" in w for w in version.warnings)
        assert any("Survivorship" in w for w in version.warnings)
        rows = session.scalars(
            select(MLDatasetObservationRecord).where(
                MLDatasetObservationRecord.dataset_version_id == version_id
            )
        ).all()
        assert rows
        assert version.observation_count == len(rows)
        for row in rows:
            # No feature input from a period after the decision; features known by then.
            assert row.last_input_period_at is not None
            assert row.last_input_period_at <= row.decision_at
            for value in row.features.values():
                assert value["available_at"] is None or value["available_at"] <= (
                    row.data_known_at.isoformat()
                )
            if row.label_status is LabelStatus.MATURED:
                assert row.label_end_at is not None
                assert row.decision_at < row.label_end_at <= row.data_known_at
        # The newest decisions' horizons had not settled: immature, never labelled.
        assert any(r.label_status is LabelStatus.IMMATURE for r in rows)
        check = verify_dataset(session, version_id, calendar=CAL, now=lab.clock.now())
        assert check.reproduced, (check.stored_sha256, check.rebuilt_sha256)


def test_training_records_everything_and_registers_a_candidate(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    result = _train(lab, version_id)
    assert result.status is RunStatus.SUCCEEDED, result.error
    with session_scope(lab.factory) as session:
        run = fetch(session, MLTrainingRunRecord, result.run_id)
        for field in (
            "periods",
            "feature_schema",
            "preprocessing",
            "parameters",
            "training_metrics",
            "validation_metrics",
            "test_metrics",
            "baseline_metrics",
            "walk_forward",
        ):
            assert getattr(run, field), field
        assert run.code_version["libraries"]["scikit-learn"]
        assert run.random_seed == run.spec["seed"]
        periods = run.periods
        assert periods is not None
        assert run.preprocessing is not None
        assert periods["train"]["end"] < periods["validation"]["start"]
        assert periods["validation"]["end"] < periods["test"]["start"]
        assert run.preprocessing["fitted_on"] == "TRAIN"
        model = fetch(session, MLModelVersionRecord, result.model_version_id)
        assert lifecycle.current_status(session, model.id) is LifecycleStatus.CANDIDATE
        assert model.output_kind in (
            OutputKind.CALIBRATED_PROBABILITY,
            OutputKind.UNCALIBRATED_SCORE,
        )
        splits = set(
            session.scalars(
                select(MLModelEvaluationRecord.split).where(
                    MLModelEvaluationRecord.training_run_id == run.id
                )
            )
        )
        assert {SplitKind.WALK_FORWARD, SplitKind.VALIDATION, SplitKind.TEST} <= splits
        assert session.scalars(
            select(MLCalibrationResultRecord).where(
                MLCalibrationResultRecord.training_run_id == run.id
            )
        ).all()


@pytest.mark.parametrize("model", [ModelType.RANDOM_FOREST, ModelType.XGBOOST])
def test_tree_models_train(lab, model):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    result = _train(lab, version_id, model, params={"n_estimators": 30})
    assert result.status is RunStatus.SUCCEEDED, result.error


def test_inference_needs_an_approved_model_and_replays_exactly(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    result = _train(lab, version_id)
    model_id = result.model_version_id
    with session_scope(lab.factory) as session:
        frame = load_frame(session, version_id).frame
        model = fetch(session, MLModelVersionRecord, model_id)
        schema = model.input_schema
    row = frame.filter(frame["label_status"] == "MATURED").row(-1, named=True)
    features = {
        item["keys"]["1Day"]: {
            "value": None if row[item["column"]] is None else repr(row[item["column"]]),
            "status": "VALID" if row[item["column"]] is not None else "UNAVAILABLE",
            "available_at": None,
        }
        for item in schema
        if item["kind"] == "FEATURE"
    }
    request = PredictionRequest(
        timeframe="1Day",
        direction=Direction.LONG,
        decision_at=row["decision_at"],
        features=features,
    )
    refused = lab.inference.predict(model_id, request, actor="test")
    assert refused.validity is PredictionValidity.REJECTED_MODEL_STATUS
    assert refused.output is None
    shadow = lab.inference.predict(
        model_id,
        PredictionRequest(**{**request.__dict__, "mode": PredictionMode.SHADOW}),
        actor="test",
    )
    assert shadow.validity is PredictionValidity.REJECTED_MODEL_STATUS
    with session_scope(lab.factory) as session:
        # Force evidence for the lifecycle test only: fixture data, not a verdict.
        session.execute(
            text("ALTER TABLE ml_model_versions DISABLE TRIGGER ml_model_versions_append_only")
        )
        session.execute(
            text("UPDATE ml_model_versions SET validation_status = 'PASSED' WHERE id = :id"),
            {"id": model_id},
        )
        session.execute(
            text("ALTER TABLE ml_model_versions ENABLE TRIGGER ml_model_versions_append_only")
        )
        for status in (LifecycleStatus.VALIDATED, LifecycleStatus.SHADOW):
            lifecycle.transition(session, model_id, status, reason="test", actor="test")
    shadow = lab.inference.predict(
        model_id,
        PredictionRequest(**{**request.__dict__, "mode": PredictionMode.SHADOW}),
        actor="test",
    )
    assert shadow.validity is PredictionValidity.VALID, shadow.problems
    replay = lab.inference.replay(shadow.prediction_id)
    assert replay["identical"], replay
    with session_scope(lab.factory) as session:
        stored = fetch(session, MLPredictionRecord, shadow.prediction_id)
        assert stored.model_status is LifecycleStatus.SHADOW
        assert stored.feature_snapshot_sha256

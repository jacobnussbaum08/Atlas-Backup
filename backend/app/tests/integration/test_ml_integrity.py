"""Phase 5A guarantees on a real PostgreSQL: point-in-time features, reproducibility,
leakage, honest verdicts and the registry lifecycle.

All data is TEST FIXTURE material (synthetic bars, origin TEST_FIXTURE).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError

from app.data_engine.enums import BarObservationKind, DataQualityStatus, DataSource, PriceAdjustment
from app.database.models import (
    MarketBarRecord,
    MLDatasetObservationRecord,
    MLModelVersionRecord,
    MLTrainingRunRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, FeatureRequest
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.datasets.builder import build_dataset
from app.ml_engine.datasets.store import verify_dataset
from app.ml_engine.enums import (
    LabelStatus,
    LifecycleStatus,
    ModelType,
    ObservationSource,
    RunStatus,
    ValidationStatus,
)
from app.ml_engine.registry import lifecycle
from app.ml_engine.training.trainer import TrainingSpec
from app.strategies.inputs import BarSeries, compute_features
from app.strategies.registry import StrategyRegistry
from app.tests.integration.ml_support import (
    CAL,
    NOW,
    build_and_save,
    fetch,
    make_ml_lab,
    seed_universe,
    spec,
)
from app.tests.integration.strategy_support import SETTLE, AlwaysShortStrategy, midnight, sessions

pytestmark = pytest.mark.integration


@pytest.fixture
def lab(fresh_engine, tmp_path):
    return make_ml_lab(fresh_engine, tmp_path / "artifacts")


def force_passed(session, model_id: uuid.UUID) -> None:
    """TEST ONLY: mark fixture evidence PASSED to exercise the lifecycle rules. The
    append-only trigger is suspended for this single statement."""
    session.execute(
        text("ALTER TABLE ml_model_versions DISABLE TRIGGER ml_model_versions_append_only")
    )
    session.execute(
        update(MLModelVersionRecord)
        .where(MLModelVersionRecord.id == model_id)
        .values(validation_status=ValidationStatus.PASSED)
    )
    session.execute(
        text("ALTER TABLE ml_model_versions ENABLE TRIGGER ml_model_versions_append_only")
    )


def test_dataset_features_equal_the_strategy_pipeline_at_each_decision(lab):
    ids = seed_universe(lab)
    dataset_spec = spec()
    version_id = build_and_save(lab, dataset_spec)
    series = BarSeries("alpaca", "iex", PriceAdjustment.SPLIT, CAL, SETTLE)
    with session_scope(lab.factory) as session:
        rows = session.scalars(
            select(MLDatasetObservationRecord)
            .where(MLDatasetObservationRecord.dataset_version_id == version_id)
            .order_by(MLDatasetObservationRecord.row_index)
        ).all()
        requests = [
            FeatureRequest(f.name, "1Day", f.params, f.optional) for f in dataset_spec.features
        ]
        resolved = REGISTRY.resolve_all(requests)
        for row in rows[:: max(1, len(rows) // 25)]:
            direct = compute_features(
                session,
                resolved,
                asset_id=row.asset_id,
                timeframe="1Day",
                data_cutoff=row.decision_at,
                known_at=row.data_known_at,
                basis=KnowledgeBasis.RECONSTRUCTED,
                series=series,
            )
            for f in resolved:
                stored = row.features[f.key.rsplit("@", 1)[0]]
                value = direct.values[f.key]
                assert stored["status"] == value.status.value
                assert stored["value"] == (None if value.value is None else repr(value.value))
    assert ids


def test_later_revisions_and_future_bars_do_not_change_a_dataset(lab):
    ids = seed_universe(lab)
    version_id = build_and_save(lab, spec())
    days = sessions(NOW, 40)
    later = NOW + timedelta(days=1)
    with session_scope(lab.factory) as session:
        # A revision of an old bar, stored after the dataset's data_known_at.
        session.execute(
            insert(MarketBarRecord),
            [
                {
                    "asset_id": ids["FXA"],
                    "provider": "alpaca",
                    "feed": "iex",
                    "provider_symbol": "FIXTURE",
                    "timeframe": "1Day",
                    "adjustment": PriceAdjustment.SPLIT,
                    "event_time": midnight(days[10]),
                    "revision": 1,
                    "open": Decimal(1),
                    "high": Decimal(2),
                    "low": Decimal("0.5"),
                    "close": Decimal(1),
                    "volume": Decimal(1),
                    "vwap": None,
                    "trade_count": 1,
                    "is_complete": True,
                    "source": DataSource.HISTORICAL_REST,
                    "observation_kind": BarObservationKind.HISTORICAL,
                    "quality_status": DataQualityStatus.VALID,
                    "received_at": later,
                    "available_at": later,
                    "original_available_at": None,
                    "processed_at": later,
                    "ingested_at": later,
                }
            ],
        )
    lab.clock.advance(timedelta(days=2))
    with session_scope(lab.factory) as session:
        check = verify_dataset(session, version_id, calendar=CAL, now=lab.clock.now())
    assert check.reproduced


def test_training_is_reproducible(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    training = TrainingSpec(
        dataset_version_id=version_id, model_type=ModelType.XGBOOST, params={"n_estimators": 40}
    )
    first = lab.trainer.train(training, actor="test")
    second = lab.trainer.train(training, actor="test")
    assert first.status is second.status is RunStatus.SUCCEEDED
    with session_scope(lab.factory) as session:
        a = fetch(session, MLTrainingRunRecord, first.run_id)
        b = fetch(session, MLTrainingRunRecord, second.run_id)
        assert a.test_metrics == b.test_metrics
        assert a.walk_forward == b.walk_forward
        assert a.preprocessing == b.preprocessing
        assert b.test_period_reuse == 1  # the same test period was evaluated before
        assert any("already evaluated" in n for n in b.notes)


def test_a_planted_signal_is_found_and_noise_is_not_called_a_signal(fresh_engine, tmp_path):
    planted = make_ml_lab(fresh_engine, tmp_path / "a")
    seed_universe(planted, planted=True)
    found = planted.trainer.train(
        TrainingSpec(
            dataset_version_id=build_and_save(planted, spec()),
            model_type=ModelType.XGBOOST,
            params={"n_estimators": 150},
        ),
        actor="test",
    )
    with session_scope(planted.factory) as session:
        run = fetch(session, MLTrainingRunRecord, found.run_id)
        assert run.test_metrics is not None
        assert run.test_metrics["roc_auc"] > 0.55, run.validation_reasons


def test_noise_is_not_validated(fresh_engine, tmp_path):
    noisy = make_ml_lab(fresh_engine, tmp_path / "b")
    seed_universe(noisy, planted=False)
    result = noisy.trainer.train(
        TrainingSpec(
            dataset_version_id=build_and_save(noisy, spec(name="fixture-noise")),
            model_type=ModelType.XGBOOST,
            params={"n_estimators": 150},
        ),
        actor="test",
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.validation_status is not ValidationStatus.PASSED


def test_strategy_replay_observations_carry_the_strategy_version(lab):
    seed_universe(lab)
    replay = spec(
        name="fixture-replay",
        source="STRATEGY_REPLAY",
        strategies=["fixture_short"],
        directions=["SHORT"],
        target={
            "kind": "BARRIER",
            "horizon_periods": None,
            "barrier": {"source": "STRATEGY_LEVELS"},
        },
    )
    with session_scope(lab.factory) as session:
        built = build_dataset(
            session,
            replay,
            provider="alpaca",
            feed="iex",
            calendar=CAL,
            now=lab.clock.now(),
            strategies=StrategyRegistry([AlwaysShortStrategy()]),
        )
    assert built.observations
    for o in built.observations:
        assert o.source is ObservationSource.STRATEGY_REPLAY
        assert o.strategy_id == "fixture_short"
        assert o.strategy_version is not None
        assert o.strategy_version.startswith("fixture_short@fixture-short-1#")
        if o.label.status is LabelStatus.MATURED:
            assert o.label.outcome["stop_level"] > o.label.outcome["profit_level"]  # SHORT levels
    assert built.statistics["strategy_parameter_sources"]["fixture_short"].startswith(
        "code defaults"
    )


def test_registry_lifecycle_rules_and_append_only_storage(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    first = lab.trainer.train(
        TrainingSpec(dataset_version_id=version_id, model_type=ModelType.LOGISTIC_REGRESSION),
        actor="test",
    )
    second = lab.trainer.train(
        TrainingSpec(
            dataset_version_id=version_id, model_type=ModelType.LOGISTIC_REGRESSION, seed=1
        ),
        actor="test",
    )
    a, b = first.model_version_id, second.model_version_id
    assert a is not None
    assert b is not None
    with session_scope(lab.factory) as session:
        assert fetch(session, MLModelVersionRecord, b).version == 2  # a new model is a new version
        with pytest.raises(lifecycle.LifecycleError, match="not an allowed"):
            lifecycle.transition(session, a, LifecycleStatus.APPROVED, reason="skip", actor="test")
    with session_scope(lab.factory) as session:
        if fetch(session, MLModelVersionRecord, a).validation_status is not ValidationStatus.PASSED:
            with pytest.raises(lifecycle.LifecycleError, match="out-of-sample evidence"):
                lifecycle.transition(
                    session, a, LifecycleStatus.VALIDATED, reason="x", actor="test"
                )
    with session_scope(lab.factory) as session:
        for model in (a, b):
            force_passed(session, model)
            for status in (LifecycleStatus.VALIDATED, LifecycleStatus.SHADOW):
                lifecycle.transition(session, model, status, reason="test", actor="test")
        lifecycle.transition(session, a, LifecycleStatus.APPROVED, reason="test", actor="test")
    with (
        session_scope(lab.factory) as session,
        pytest.raises(lifecycle.LifecycleError, match="already has an APPROVED"),
    ):
        lifecycle.transition(session, b, LifecycleStatus.APPROVED, reason="x", actor="test")
    with session_scope(lab.factory) as session:
        lifecycle.transition(session, a, LifecycleStatus.RETIRED, reason="replaced", actor="test")
        lifecycle.transition(session, b, LifecycleStatus.APPROVED, reason="explicit", actor="test")
        assert lifecycle.current_status(session, a) is LifecycleStatus.RETIRED
        assert lifecycle.approved_versions(
            session, fetch(session, MLModelVersionRecord, b).model_name
        ) == [b]
    with pytest.raises(DBAPIError, match="append-only"), session_scope(lab.factory) as session:
        session.execute(update(MLModelVersionRecord).values(version=99))
    with pytest.raises(DBAPIError, match="final"), session_scope(lab.factory) as session:
        session.execute(
            update(MLTrainingRunRecord)
            .where(MLTrainingRunRecord.id == first.run_id)
            .values(notes=["rewritten"])
        )

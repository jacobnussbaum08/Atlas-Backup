"""Phase 5A ML API against a real PostgreSQL. All data is TEST FIXTURE material."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database.sessions import session_scope
from app.main import create_app
from app.ml_engine.enums import ModelType
from app.ml_engine.inference.engine import PredictionRequest
from app.ml_engine.runtime import build_ml_context
from app.ml_engine.training.trainer import TrainingSpec
from app.strategies.enums import Direction
from app.tests.integration.ml_support import build_and_save, make_ml_lab, seed_universe, spec

pytestmark = pytest.mark.integration
BASE = "/api/v1/ml"


@pytest.fixture
def lab(fresh_engine, tmp_path):
    return make_ml_lab(fresh_engine, tmp_path / "artifacts")


@pytest.fixture
def trained(lab):
    seed_universe(lab)
    version_id = build_and_save(lab, spec())
    result = lab.trainer.train(
        TrainingSpec(dataset_version_id=version_id, model_type=ModelType.LOGISTIC_REGRESSION),
        actor="test",
    )
    return version_id, result


@pytest.fixture
def client(lab, make_settings):
    def build(**settings) -> TestClient:
        ctx = build_ml_context(
            lab.factory, lab.clock, provider="alpaca", feed="iex", settings=lab.settings
        )
        app = create_app(make_settings(**settings), clock=lab.clock, health_checks={}, ml=ctx)
        return TestClient(app)

    return build


def test_overview_and_empty_lists(client):
    api = client()
    body = api.get(f"{BASE}/overview").json()
    assert body["datasets"] == 0
    assert any(
        "cannot submit orders" in note or "no model can submit orders" in note
        for note in body["notes"]
    )
    assert api.get(f"{BASE}/models").json() == []
    assert api.get(f"{BASE}/runs").json() == []
    assert api.get(f"{BASE}/predictions").json() == []


def test_registry_runs_datasets_and_comparison(client, trained):
    version_id, result = trained
    api = client()
    datasets = api.get(f"{BASE}/datasets").json()
    assert datasets[0]["origin"] == "TEST_FIXTURE"
    detail = api.get(f"{BASE}/datasets/{version_id}").json()
    assert detail["target_definition"]["documentation"]
    assert detail["runs"] == 1
    runs = api.get(f"{BASE}/runs").json()
    assert runs[0]["id"] == str(result.run_id)
    assert runs[0]["origin"] == "TEST_FIXTURE"
    run = api.get(f"{BASE}/runs/{result.run_id}").json()
    assert {e["split"] for e in run["evaluations"]} >= {"WALK_FORWARD", "VALIDATION", "TEST"}
    assert run["periods"]["test"]["observations"] > 0
    models = api.get(f"{BASE}/models").json()
    assert models[0]["status"] == "CANDIDATE"
    model = api.get(f"{BASE}/models/{result.model_version_id}").json()
    assert model["allowed_transitions"] == ["REJECTED", "VALIDATED"]
    assert model["artifact"]["sha256"]
    compare = api.get(f"{BASE}/compare", params={"run_ids": [str(result.run_id)]}).json()
    assert compare["comparable"] is True
    assert api.get(f"{BASE}/runs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_transitions_need_an_operator_and_evidence(client, trained, lab):
    _, result = trained
    model_id = result.model_version_id
    api = client(operator_token="op-secret")
    body = {"status": "REJECTED", "reason": "fixture model"}
    assert api.post(f"{BASE}/models/{model_id}/status", json=body).status_code == 401
    skip = api.post(
        f"{BASE}/models/{model_id}/status",
        json={"status": "APPROVED", "reason": "skip the lifecycle"},
        headers={"X-Atlas-Operator-Token": "op-secret"},
    )
    assert skip.status_code == 409
    assert skip.json()["error"]["code"] == "ml_lifecycle_conflict"
    done = api.post(
        f"{BASE}/models/{model_id}/status",
        json=body,
        headers={"X-Atlas-Operator-Token": "op-secret"},
    )
    assert done.status_code == 200
    assert done.json()["model"]["status"] == "REJECTED"
    assert done.json()["previous_status"] == "CANDIDATE"
    with session_scope(lab.factory) as session:
        audited = session.execute(
            text("SELECT count(*) FROM audit_logs WHERE action = 'ml_model.status_changed'")
        ).scalar_one()
    assert audited == 1


def test_prediction_history_and_replay(client, trained, lab):
    _, result = trained
    outcome = lab.inference.predict(
        result.model_version_id,
        PredictionRequest(
            timeframe="1Day", direction=Direction.LONG, decision_at=lab.clock.now(), features={}
        ),
        actor="test",
    )
    api = client()
    rows = api.get(f"{BASE}/predictions").json()
    assert rows[0]["id"] == str(outcome.prediction_id)
    assert rows[0]["validity"] == "REJECTED_MODEL_STATUS"
    assert rows[0]["output"] is None
    detail = api.get(f"{BASE}/predictions/{outcome.prediction_id}").json()
    assert detail["feature_snapshot_sha256"]
    replay = api.get(f"{BASE}/predictions/{outcome.prediction_id}/replay").json()
    assert replay["replayable"] is False

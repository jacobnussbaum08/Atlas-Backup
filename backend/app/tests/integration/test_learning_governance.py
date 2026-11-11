"""Phase 8B: champion/challenger lifecycle, approval gates, rollback, degradation monitoring
and the validation API against PostgreSQL.

Validation runs, holdout evaluations, shadow evaluations and model evidence marked
``TEST FIXTURE`` are inserted directly to exercise the gates; they are not results.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.backtesting.config import sha256_of
from app.database.models import AuditLog, StrategyVersionRecord
from app.database.models.learning import (
    LearningApprovalRecord,
    LearningDegradationAlertRecord,
    LearningDriftMeasurementRecord,
    ValidationComparisonRecord,
    ValidationExperimentRecord,
    ValidationHoldoutRecord,
    ValidationRunRecord,
)
from app.database.sessions import session_scope
from app.learning import governance as gov
from app.learning.enums import (
    CandidateStatus,
    ExperimentKind,
    MeasurementStatus,
    MonitorMetric,
    RuleAction,
    RunStatus,
    SearchMethod,
    SubjectKind,
    Verdict,
)
from app.learning.governance import GovernanceError
from app.learning.monitoring import Measurement, MonitorError, MonitorRule
from app.main import create_app
from app.ml_engine.enums import LifecycleStatus, ModelType
from app.ml_engine.registry import lifecycle as ml
from app.ml_engine.training.trainer import TrainingSpec
from app.strategies import catalog
from app.tests.integration.ml_support import build_and_save, make_ml_lab, seed_universe, spec
from app.tests.integration.validation_support import ValidationLab, experiment, make_validation_lab

pytestmark = pytest.mark.integration
OP = "operator:test"


@pytest.fixture
def lab(fresh_engine: Engine) -> ValidationLab:
    return make_validation_lab(fresh_engine)


def _params(lab: ValidationLab, **changes: Any) -> dict[str, Any]:
    with session_scope(lab.bench.factory) as session:
        live = catalog.latest_version(session, "fixture_proposer")
        assert live is not None
        base = dict(catalog.configuration(session, live).parameters)
    strategy = lab.bench.registry.get("fixture_proposer")
    return catalog.parameters_json(strategy.parse_parameters({**base, **changes}))


def fixture_run(
    lab: ValidationLab, params: dict[str, Any], verdict: Verdict, *, holdout: bool
) -> uuid.UUID:
    """TEST FIXTURE: a completed validation run record with the given verdict."""
    with session_scope(lab.bench.factory) as session:
        exp = ValidationExperimentRecord(
            name="TEST FIXTURE run",
            kind=ExperimentKind.WALK_FORWARD,
            config={"fixture": True},
            config_sha256="0" * 64,
            strategy_ids=["fixture_proposer"],
            search_method=SearchMethod.GRID,
            max_trials=1,
            created_by="test",
        )
        session.add(exp)
        session.flush()
        run = ValidationRunRecord(
            experiment_id=exp.id,
            status=RunStatus.QUEUED,
            base_config={"fixture": True},
            base_config_sha256="0" * 64,
            engine_version="test",
            code_version="test",
            seed=0,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 10, 28),
            data_known_at=datetime(2026, 11, 1, tzinfo=UTC),
            requested_by="test",
            holdout_start=date(2026, 9, 1),
            holdout_end=date(2026, 10, 28),
        )
        session.add(run)
        session.flush()
        run_id = run.id
    with session_scope(lab.bench.factory) as session:
        session.execute(
            update(ValidationRunRecord)
            .where(ValidationRunRecord.id == run_id)
            .values(status=RunStatus.RUNNING)
        )
    with session_scope(lab.bench.factory) as session:
        session.execute(
            update(ValidationRunRecord)
            .where(ValidationRunRecord.id == run_id)
            .values(
                status=RunStatus.COMPLETED,
                verdict=verdict,
                result_sha256="1" * 64,
                summary={
                    "final_parameters": {"fixture_proposer": params},
                    "final_parameters_sha256": {"fixture_proposer": sha256_of(params)},
                    "note": "TEST FIXTURE",
                },
            )
        )
    if holdout:
        with session_scope(lab.bench.factory) as session:
            session.add(
                ValidationHoldoutRecord(
                    run_id=run_id,
                    strategy_ids=["fixture_proposer"],
                    holdout_start=date(2026, 9, 1),
                    holdout_end=date(2026, 10, 28),
                    parameters={"fixture_proposer": params},
                    parameters_sha256=sha256_of({"fixture_proposer": params}),
                    access_number=1,
                    result={"net_return": 0.01, "note": "TEST FIXTURE"},
                    config_sha256="0" * 64,
                    dataset_sha256="0" * 64,
                    requested_by="test",
                )
            )
    return run_id


def fixture_shadow(
    lab: ValidationLab, candidate_id: uuid.UUID, kind: SubjectKind = SubjectKind.STRATEGY
) -> None:
    result = {"sufficient": True, "sessions": 25, "note": "TEST FIXTURE shadow evidence"}
    with session_scope(lab.bench.factory) as session:
        session.add(
            ValidationComparisonRecord(
                kind=kind,
                title="TEST FIXTURE shadow",
                subjects={"candidate_id": str(candidate_id), "shadow": True},
                result=result,
                result_sha256=sha256_of(result),
                created_by="test",
            )
        )


def _status(lab: ValidationLab, cid: uuid.UUID) -> CandidateStatus:
    with session_scope(lab.bench.factory) as session:
        return gov.status_of(session, cid)


def _versions(lab: ValidationLab) -> list[tuple[int, dict[str, Any]]]:
    with session_scope(lab.bench.factory) as session:
        rows = session.scalars(
            select(StrategyVersionRecord)
            .where(StrategyVersionRecord.strategy_id == "fixture_proposer")
            .order_by(StrategyVersionRecord.version)
        ).all()
        return [(v.version, dict(catalog.configuration(session, v).parameters)) for v in rows]


def test_a_strategy_challenger_needs_evidence_and_an_explicit_replacement_and_can_roll_back(
    lab: ValidationLab,
) -> None:
    g = lab.ctx.governance
    original = _versions(lab)
    assert len(original) == 1
    incumbent = g.adopt_incumbent("fixture_proposer", actor=OP, reason="record the live version")
    assert _status(lab, incumbent) is CandidateStatus.APPROVED
    assert _versions(lab) == original  # adoption changes nothing that trades

    params = _params(lab, sma_length=10)
    cid = g.create_strategy_candidate(
        "fixture_proposer", {"sma_length": 10}, actor=OP, reason="challenger"
    )
    assert _versions(lab) == original  # a challenger is not a strategy version
    with pytest.raises(GovernanceError, match="VALIDATING"):
        g.to_shadow(cid, actor=OP, reason="skip validation")
    g.start_validation(cid, actor=OP, reason="validate it")
    with pytest.raises(GovernanceError, match="name the validation run"):
        g.to_shadow(cid, actor=OP, reason="no evidence")
    weak = fixture_run(lab, params, Verdict.INSUFFICIENT_EVIDENCE, holdout=True)
    with pytest.raises(GovernanceError, match="verdict is INSUFFICIENT_EVIDENCE"):
        g.to_shadow(cid, actor=OP, reason="weak evidence", run_id=weak)
    other = fixture_run(lab, _params(lab, sma_length=3), Verdict.CANDIDATE_FOR_REVIEW, holdout=True)
    with pytest.raises(GovernanceError, match="not the candidate's"):
        g.to_shadow(cid, actor=OP, reason="other parameters", run_id=other)
    no_holdout = fixture_run(lab, params, Verdict.CANDIDATE_FOR_REVIEW, holdout=False)
    with pytest.raises(GovernanceError, match="holdout has not been evaluated"):
        g.to_shadow(cid, actor=OP, reason="holdout unread", run_id=no_holdout)
    good = fixture_run(lab, params, Verdict.CANDIDATE_FOR_REVIEW, holdout=True)
    g.to_shadow(cid, actor=OP, reason="validated out of sample", run_id=good)
    assert _status(lab, cid) is CandidateStatus.SHADOW

    with pytest.raises(GovernanceError, match="sufficient shadow evaluation"):
        g.approve(cid, actor=OP, reason="too early", replaces=incumbent)
    fixture_shadow(lab, cid)
    with pytest.raises(GovernanceError, match="never replaced implicitly"):
        g.approve(cid, actor=OP, reason="replace silently")
    approval = g.approve(cid, actor=OP, reason="human approval", replaces=incumbent)
    assert _status(lab, cid) is CandidateStatus.APPROVED
    assert _status(lab, incumbent) is CandidateStatus.RETIRED
    versions = _versions(lab)
    assert [v for v, _ in versions] == [1, 2]
    assert versions[1][1] == params  # the approved challenger is the new live version

    rollback = g.rollback(incumbent, actor=OP, reason="roll back to the incumbent")
    assert _status(lab, incumbent) is CandidateStatus.APPROVED
    assert _status(lab, cid) is CandidateStatus.RETIRED
    versions = _versions(lab)
    assert [v for v, _ in versions] == [1, 2, 3]  # history kept: a rollback is a new version
    assert versions[2][1] == original[0][1]
    with session_scope(lab.bench.factory) as session:
        rows = {r.id: r for r in session.scalars(select(LearningApprovalRecord))}
        assert rows[approval].decision.value == "APPROVE"
        assert rows[approval].replaced == {"candidate_id": str(incumbent), "model_version_id": None}
        assert "shadow" in rows[approval].evidence
        assert rows[rollback].decision.value == "ROLLBACK"
        audited = session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "learning.candidate_status_changed"
            )
        )
        assert audited
        assert audited >= 8
    with pytest.raises(GovernanceError, match="rollback needs"):
        g.rollback(incumbent, actor=OP, reason="the incumbent is APPROVED, not RETIRED")


def test_rejection_is_final_and_retiring_a_champion_disables_the_strategy(
    lab: ValidationLab,
) -> None:
    g = lab.ctx.governance
    cid = g.create_strategy_candidate("fixture_proposer", {"sma_length": 3}, actor=OP, reason="try")
    g.start_validation(cid, actor=OP, reason="validate")
    g.reject(cid, actor=OP, reason="not robust")
    for action in (g.start_validation, g.reject):
        with pytest.raises(GovernanceError, match="not an allowed transition"):
            action(cid, actor=OP, reason="again")
    incumbent = g.adopt_incumbent("fixture_proposer", actor=OP, reason="adopt")
    with session_scope(lab.bench.factory) as session:
        catalog.set_status(
            session, lab.bench.registry.get("fixture_proposer"), enabled=True, reason="t", actor="t"
        )
    g.retire(incumbent, actor=OP, reason="stop it")
    with session_scope(lab.bench.factory) as session:
        assert catalog.is_enabled(session, "fixture_proposer") is False


def test_lifecycle_and_approval_records_are_append_only(lab: ValidationLab) -> None:
    g = lab.ctx.governance
    cid = g.create_strategy_candidate("fixture_proposer", {"sma_length": 3}, actor=OP, reason="try")
    g.reject(cid, actor=OP, reason="no")
    for sql in (
        "UPDATE learning_candidate_events SET to_status = 'APPROVED' WHERE candidate_id = :id",
        "DELETE FROM learning_candidate_events WHERE candidate_id = :id",
        "UPDATE learning_candidates SET parameters = '{}' WHERE id = :id",
        "DELETE FROM learning_approvals WHERE candidate_id = :id",
    ):
        with pytest.raises(DBAPIError), session_scope(lab.bench.factory) as session:
            session.execute(text(sql), {"id": cid})


def test_a_model_challenger_moves_the_5a_registry_and_rollback_reinstates_it(
    lab: ValidationLab, tmp_path: Path
) -> None:
    ml_lab = make_ml_lab(lab.bench.factory.kw["bind"], tmp_path / "artifacts")
    seed_universe(ml_lab)
    dataset = build_and_save(ml_lab, spec())
    trained = ml_lab.trainer.train(
        TrainingSpec(dataset_version_id=dataset, model_type=ModelType.LOGISTIC_REGRESSION),
        actor="test",
    )
    model_id = trained.model_version_id
    assert model_id is not None
    g = lab.ctx.governance
    cid = g.create_model_candidate(model_id, actor=OP, reason="challenger model")
    g.start_validation(cid, actor=OP, reason="evaluate")
    with session_scope(lab.bench.factory) as session:
        # TEST ONLY: fixture evidence, as in the 5A lifecycle tests (trigger suspended for
        # this one statement).
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
    with pytest.raises(GovernanceError, match="no 8B model comparison"):
        g.to_shadow(cid, actor=OP, reason="no comparison")
    with session_scope(lab.bench.factory) as session:
        run_id = session.scalar(
            text("SELECT training_run_id FROM ml_model_versions WHERE id = :id"), {"id": model_id}
        )
        result = {
            "models": [
                {"training_run_id": str(run_id), "incremental_value": True, "note": "TEST FIXTURE"}
            ]
        }
        session.add(
            ValidationComparisonRecord(
                kind=SubjectKind.MODEL,
                title="TEST FIXTURE comparison",
                subjects={},
                result=result,
                result_sha256=sha256_of(result),
                created_by="test",
            )
        )
    g.to_shadow(cid, actor=OP, reason="evidence recorded")
    with session_scope(lab.bench.factory) as session:
        assert ml.current_status(session, model_id) is LifecycleStatus.SHADOW
    fixture_shadow(lab, cid, SubjectKind.MODEL)
    g.approve(cid, actor=OP, reason="approved by an operator")
    with session_scope(lab.bench.factory) as session:
        assert ml.current_status(session, model_id) is LifecycleStatus.APPROVED
    g.retire(cid, actor=OP, reason="retire")
    g.rollback(cid, actor=OP, reason="reinstate it")
    with session_scope(lab.bench.factory) as session:
        assert ml.current_status(session, model_id) is LifecycleStatus.APPROVED
        events = (
            session.execute(
                text(
                    "SELECT to_status FROM ml_model_status_events "
                    "WHERE model_version_id = :id ORDER BY id"
                ),
                {"id": model_id},
            )
            .scalars()
            .all()
        )
        assert events == ["CANDIDATE", "VALIDATED", "SHADOW", "APPROVED", "RETIRED", "APPROVED"]


def test_the_monitor_warns_and_suspends_only_under_an_approved_rule(lab: ValidationLab) -> None:
    mon = lab.ctx.monitoring
    with pytest.raises(MonitorError, match="only a strategy"):
        mon.approve_rule(
            SubjectKind.MODEL, "m", MonitorRule(), RuleAction.SUSPEND, actor=OP, reason="x"
        )
    with session_scope(lab.bench.factory) as session:
        catalog.set_status(
            session, lab.bench.registry.get("fixture_proposer"), enabled=True, reason="t", actor="t"
        )
    # No evidence anywhere: every measurement is INSUFFICIENT_DATA and nothing alerts.
    monitor_id, report = mon.run()
    with session_scope(lab.bench.factory) as session:
        rows = session.scalars(
            select(LearningDriftMeasurementRecord).where(
                LearningDriftMeasurementRecord.monitor_run_id == monitor_id
            )
        ).all()
        assert rows
        assert all(r.status is MeasurementStatus.INSUFFICIENT_DATA for r in rows)
    assert report["subjects"][0]["alerts"] == []

    breach = Measurement(
        MonitorMetric.EXPECTANCY_SHIFT, MeasurementStatus.BREACH, -4.2, 0.0001, 60, 60, {}, {}, {}
    )
    drift = Measurement(
        MonitorMetric.FEATURE_PSI,
        MeasurementStatus.BREACH,
        0.6,
        None,
        500,
        500,
        {},
        {},
        {},
        feature="rsi",
    )

    def store(
        rule_action: RuleAction | None, measurements: list[Measurement]
    ) -> list[dict[str, Any]]:
        with session_scope(lab.bench.factory) as session:
            rule = None
            if rule_action is not None:
                mon.approve_rule(
                    SubjectKind.STRATEGY,
                    "fixture_proposer",
                    MonitorRule(),
                    rule_action,
                    actor=OP,
                    reason="approved",
                )
                rule = mon.active_rule(session, SubjectKind.STRATEGY, "fixture_proposer")
            return mon._store(
                session,
                uuid.uuid4(),
                SubjectKind.STRATEGY,
                "fixture_proposer",
                datetime.now(UTC),
                measurements,
                rule,
            )

    warned = store(None, [breach])
    assert [a["action"] for a in warned] == ["WARNED"]
    with session_scope(lab.bench.factory) as session:
        assert catalog.is_enabled(session, "fixture_proposer")
    # Feature drift is a warning even under a SUSPEND rule: drift is not failure.
    drift_only = store(RuleAction.SUSPEND, [drift])
    assert [a["action"] for a in drift_only] == ["WARNED"]
    with session_scope(lab.bench.factory) as session:
        assert catalog.is_enabled(session, "fixture_proposer")
    suspended = store(RuleAction.SUSPEND, [breach])
    assert [a["action"] for a in suspended] == ["SUSPENDED"]
    with session_scope(lab.bench.factory) as session:
        assert not catalog.is_enabled(session, "fixture_proposer")
        status = catalog.current_status(session, "fixture_proposer")
        assert status is not None
        assert "monitoring rule" in status.reason
        alerts = session.scalars(select(LearningDegradationAlertRecord)).all()
        assert {a.action.value for a in alerts} == {"WARNED", "SUSPENDED"}
        # Rules are versioned; the latest active one applies.
        versions = session.scalars(
            text("SELECT version FROM learning_monitoring_rules ORDER BY version")
        ).all()
        assert versions == [1, 2]


@pytest.fixture
def api(lab: ValidationLab, make_settings: Any) -> TestClient:
    app = create_app(
        make_settings(operator_token="op-secret"), health_checks={}, validation=lab.ctx
    )
    return TestClient(app)


def test_the_api_runs_validations_and_records_decisions_only_with_an_operator_token(
    api: TestClient, lab: ValidationLab
) -> None:
    H = {"X-Atlas-Operator-Token": "op-secret"}
    body = experiment(
        search="RANDOM",
        max_trials=2,
        robustness={
            "bootstrap_resamples": 100,
            "monte_carlo_resamples": 100,
            "cost_multipliers": ["2"],
            "slippage_multipliers": ["2"],
            "pbo_groups": 4,
            "cpcv_groups": 4,
            "purged_kfold": 4,
            "sensitivity": False,
        },
    )
    with api:
        assert api.post("/api/v1/validation/runs", json=body).status_code == 401
        assert (
            api.post(
                "/api/v1/validation/runs", json={**body, "max_trials": 0}, headers=H
            ).status_code
            == 422
        )
        created = api.post("/api/v1/validation/runs", json=body, headers=H)
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        assert created.json()["holdout_start"] is not None  # reserved before anything runs
        lab.ctx.drain()
        detail = api.get(f"/api/v1/validation/runs/{run_id}").json()
        assert detail["status"] == "COMPLETED", detail["error_message"]
        assert detail["holdout_status"] == "UNTOUCHED"
        for path in ("folds", "trials", "robustness", "segments", "sensitivity"):
            response = api.get(f"/api/v1/validation/runs/{run_id}/{path}")
            assert response.status_code == 200, path
        assert (
            api.get(f"/api/v1/validation/compare?run_ids={run_id}").json()["runs"][0]["id"]
            == run_id
        )
        assert api.post(f"/api/v1/validation/runs/{run_id}/holdout").status_code == 401
        holdout = api.post(f"/api/v1/validation/runs/{run_id}/holdout", headers=H)
        assert holdout.status_code == 200
        assert holdout.json()["access_number"] == 1
        assert api.get("/api/v1/validation/overview").json()["holdout_evaluations"] == 1
        cand = api.post(
            "/api/v1/validation/candidates",
            json={
                "subject_kind": "STRATEGY",
                "strategy_id": "fixture_proposer",
                "parameters": {"sma_length": 3},
                "reason": "try",
            },
            headers=H,
        )
        assert cand.status_code == 201, cand.text
        cid = cand.json()["id"]
        skip = api.post(
            f"/api/v1/validation/candidates/{cid}/transition",
            json={"action": "approve", "reason": "skip everything"},
            headers=H,
        )
        assert skip.status_code == 409
        assert skip.json()["error"]["code"] == "learning_governance_conflict"
        rejected = api.post(
            f"/api/v1/validation/candidates/{cid}/transition",
            json={"action": "reject", "reason": "not robust"},
            headers=H,
        )
        assert rejected.json()["status"] == "REJECTED"
        assert [p["decision"] for p in api.get("/api/v1/validation/promotions").json()] == [
            "REJECT"
        ]
        champions = api.get("/api/v1/validation/champions").json()
        assert champions[0]["label"].startswith("INCUMBENT")
        with session_scope(lab.bench.factory) as session:
            catalog.set_status(
                session,
                lab.bench.registry.get("fixture_proposer"),
                enabled=True,
                reason="t",
                actor="t",
            )
        assert api.post("/api/v1/validation/monitoring/run").status_code == 401
        monitored = api.post("/api/v1/validation/monitoring/run", headers=H)
        assert monitored.status_code == 200
        assert monitored.json()["subjects"][0]["subject_key"] == "fixture_proposer"
        assert api.get("/api/v1/validation/monitoring").json()["latest_monitor_run_id"]
        assert api.get("/api/v1/validation/models/compare").status_code == 200

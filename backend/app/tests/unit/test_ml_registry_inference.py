"""Phase 5A registry rules, artifacts, inference input validation, specifications and
module boundaries (pure)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.enums import LifecycleStatus, PredictionValidity
from app.ml_engine.inference.engine import PredictionRequest, validate_inputs
from app.ml_engine.registry.artifacts import (
    ARTIFACT_FORMAT,
    ArtifactError,
    read_artifact,
    write_artifact,
)
from app.ml_engine.registry.lifecycle import TRANSITIONS
from app.strategies.enums import Direction

S = LifecycleStatus
DECISION = datetime(2026, 10, 30, 21, tzinfo=UTC)


def test_lifecycle_never_skips_shadow_and_final_states_are_final():
    assert S.APPROVED not in TRANSITIONS[S.CANDIDATE]
    assert S.APPROVED not in TRANSITIONS[S.VALIDATED]
    assert TRANSITIONS[S.SHADOW] >= {S.APPROVED}
    assert TRANSITIONS[S.REJECTED] == frozenset()
    assert TRANSITIONS[S.RETIRED] == frozenset()
    assert all(S.CANDIDATE not in targets for targets in TRANSITIONS.values())


def test_artifacts_are_verified_and_never_overwritten(tmp_path: Path):
    written = write_artifact(tmp_path, "m/v1.joblib", {"format": ARTIFACT_FORMAT, "x": 1})
    assert read_artifact(tmp_path, written.path, written.sha256)["x"] == 1
    with pytest.raises(FileExistsError):
        write_artifact(tmp_path, "m/v1.joblib", {"format": ARTIFACT_FORMAT})
    (tmp_path / "m/v1.joblib").write_bytes(b"tampered")
    with pytest.raises(ArtifactError, match="sha256"):
        read_artifact(tmp_path, written.path, written.sha256)
    with pytest.raises(ArtifactError, match="escapes"):
        read_artifact(tmp_path, "../outside.joblib", written.sha256)


SCHEMA = [
    {
        "column": "rsi.v1(length=14)",
        "kind": "FEATURE",
        "keys": {"1Day": "rsi.v1(length=14)@1Day"},
        "value_kind": "NUMBER",
        "missing_allowed": False,
    },
    {
        "column": "volume_ratio.v1(long=20,short=5)",
        "kind": "FEATURE",
        "keys": {"1Day": "volume_ratio.v1(long=20,short=5)@1Day"},
        "value_kind": "NUMBER",
        "missing_allowed": True,
    },
    {"column": "direction", "kind": "CONTEXT", "value_kind": "LABEL", "missing_allowed": False},
]


def request(features, timeframe="1Day") -> PredictionRequest:
    return PredictionRequest(
        timeframe=timeframe, direction=Direction.SHORT, decision_at=DECISION, features=features
    )


def entry(value, status="VALID", available=DECISION - timedelta(hours=1)):
    return {"value": value, "status": status, "available_at": available.isoformat()}


def test_valid_inputs_and_optional_missing_feature():
    validity, problems, row, snapshot = validate_inputs(
        SCHEMA,
        request(
            {
                "rsi.v1(length=14)@1Day": entry("55.5"),
                "volume_ratio.v1(long=20,short=5)@1Day": entry(None, "UNAVAILABLE"),
            }
        ),
    )
    assert validity is PredictionValidity.VALID, problems
    assert row == {
        "rsi.v1(length=14)": 55.5,
        "volume_ratio.v1(long=20,short=5)": None,
        "direction": "SHORT",
    }
    assert snapshot["context:direction"] == "SHORT"


def test_missing_or_incompatible_features_are_rejected():
    validity, problems, *_ = validate_inputs(SCHEMA, request({"rsi.v2(length=14)@1Day": entry(50)}))
    assert validity is PredictionValidity.REJECTED_SCHEMA
    assert any("incompatible" in p for p in problems)
    validity, problems, *_ = validate_inputs(
        SCHEMA,
        request(
            {
                "rsi.v1(length=14)@1Day": entry(None, "INSUFFICIENT_DATA"),
                "volume_ratio.v1(long=20,short=5)@1Day": entry(1.0),
            }
        ),
    )
    assert validity is PredictionValidity.REJECTED_SCHEMA
    validity, _, *_ = validate_inputs(SCHEMA, request({}, timeframe="1Week"))
    assert validity is PredictionValidity.REJECTED_SCHEMA


@pytest.mark.parametrize(
    "bad",
    [
        entry("nan"),
        entry(float("inf")),
        entry("abc"),
        entry(True),
        entry(50, available=DECISION + timedelta(minutes=1)),  # known only after the decision
    ],
)
def test_invalid_values_are_rejected(bad):
    validity, problems, *_ = validate_inputs(
        SCHEMA,
        request(
            {"rsi.v1(length=14)@1Day": bad, "volume_ratio.v1(long=20,short=5)@1Day": entry(1.0)}
        ),
    )
    assert validity is PredictionValidity.REJECTED_INVALID_VALUES, problems


BASE_SPEC = {
    "name": "spec-test",
    "symbols": ["aapl", "AAPL", "MSFT"],
    "decision_start": "2026-01-01T00:00:00Z",
    "decision_end": "2026-06-01T00:00:00Z",
    "features": [{"name": "rsi"}],
    "target": {"kind": "DIRECTION"},
}


def test_spec_normalizes_and_hashes_deterministically():
    spec = DatasetSpec.model_validate(BASE_SPEC)
    assert spec.symbols == ("AAPL", "MSFT")
    assert spec.sha256 == DatasetSpec.model_validate(BASE_SPEC).sha256
    assert spec.target.definition()["documentation"]


@pytest.mark.parametrize(
    "change",
    [
        {"decision_end": "2025-12-01T00:00:00Z"},
        {"decision_start": "2026-01-01T00:00:00"},  # naive timestamp
        {"data_known_at": "2026-05-01T00:00:00Z"},  # before decision_end
        {"timeframes": ["1Min"]},
        {"target": {"kind": "BARRIER"}},  # barrier settings missing
        {"target": {"kind": "DIRECTION", "horizon_periods": None}},
        {"features": [{"name": "rsi"}, {"name": "rsi"}]},
        {"strategies": ["momentum"]},  # PERIOD_CLOSE takes no strategies
    ],
)
def test_invalid_specs_are_refused(change):
    with pytest.raises(ValidationError):
        DatasetSpec.model_validate({**BASE_SPEC, **change})


FORBIDDEN = ("app.execution", "app.risk", "app.paper_broker", "app.portfolio", "app.accounting")


def test_the_ml_engine_cannot_reach_execution_or_risk_code():
    root = Path(__file__).resolve().parents[2] / "ml_engine"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(FORBIDDEN), f"{path.name} imports {name}"

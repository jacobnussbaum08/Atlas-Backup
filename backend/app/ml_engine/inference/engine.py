"""Model inference for future Atlas components (Phase 5A).

:class:`InferenceEngine` loads a registered model version's artifact (only after checking
its sha256), validates the incoming features against the version's input schema, predicts
and records every request in ``ml_predictions``: the model version and its status at the
time, the exact inputs and their hash, the output and its kind, and the validity. Rejected
requests are recorded too, with the problems found.

* ``APPROVED`` mode serves only the version currently APPROVED; ``SHADOW`` mode serves only
  SHADOW versions (recorded, never acted on). Any other status is refused.
* Features are matched by their full canonical key (name, version, parameters and
  timeframe): a different version or parameterization is an incompatible feature.
  A missing feature, or a non-VALID one the model was not trained to accept, rejects the
  request; non-finite or wrongly typed values reject it; values that became available
  after the decision time reject it.
* Outputs are labelled: a probability only for CALIBRATED_PROBABILITY models; otherwise an
  uncalibrated score or a regression estimate.
* :meth:`InferenceEngine.replay` recomputes a stored prediction from its stored inputs and
  artifact; identical output is the reproducibility check.

This module produces numbers and records them. It has no access to orders, positions or
risk settings, and nothing it returns authorizes a trade.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import ExchangeCalendar
from app.core.clocks import Clock
from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    MLDatasetVersionRecord,
    MLModelArtifactRecord,
    MLModelVersionRecord,
    MLPredictionRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, FeatureRequest, sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.config import MLSettings
from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.enums import (
    LifecycleStatus,
    OutputKind,
    PredictionMode,
    PredictionValidity,
    TaskKind,
)
from app.ml_engine.features.preprocessing import FittedPreprocessor
from app.ml_engine.registry import lifecycle
from app.ml_engine.registry.artifacts import ArtifactError, read_artifact
from app.strategies.enums import Direction
from app.strategies.inputs import BarSeries, compute_features

MODE_STATUS = {
    PredictionMode.APPROVED: LifecycleStatus.APPROVED,
    PredictionMode.SHADOW: LifecycleStatus.SHADOW,
}


class InferenceError(LookupError):
    """No such model, or no approved version for a model name."""


@dataclass(frozen=True)
class PredictionRequest:
    timeframe: str
    direction: Direction
    decision_at: datetime
    # Full feature key -> {"value", "status", "available_at"} (a feature snapshot entry).
    features: Mapping[str, Mapping[str, Any]]
    asset_id: uuid.UUID | None = None
    strategy_id: str | None = None
    mode: PredictionMode = PredictionMode.APPROVED


@dataclass(frozen=True)
class PredictionResult:
    prediction_id: uuid.UUID
    model_version_id: uuid.UUID
    validity: PredictionValidity
    problems: list[str]
    output_kind: OutputKind
    output: dict[str, Any] | None


@dataclass
class LoadedModel:
    version: uuid.UUID
    sha256: str
    model: Any
    preprocessor: FittedPreprocessor
    calibrator: Any
    task: TaskKind
    output_kind: OutputKind
    schema: list[dict[str, Any]]


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def validate_inputs(
    schema: list[dict[str, Any]], request: PredictionRequest
) -> tuple[PredictionValidity, list[str], dict[str, Any], dict[str, Any]]:
    """(validity, problems, model row by column, the snapshot of the inputs used)."""
    row: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}
    schema_problems: list[str] = []
    value_problems: list[str] = []
    for item in schema:
        column = item["column"]
        if item["kind"] == "CONTEXT":
            value = {
                "direction": request.direction.value,
                "timeframe": request.timeframe,
                "strategy_id": request.strategy_id or "none",
            }[column]
            row[column] = value
            snapshot[f"context:{column}"] = value
            continue
        key = item["keys"].get(request.timeframe)
        if key is None:
            schema_problems.append(f"{column} is not defined on {request.timeframe}")
            continue
        entry = request.features.get(key)
        if entry is None:
            near = [k for k in request.features if k.split(".", 1)[0] == column.split(".", 1)[0]]
            hint = f" (received incompatible {', '.join(sorted(near))})" if near else ""
            schema_problems.append(f"missing feature {key}{hint}")
            continue
        snapshot[key] = {
            "value": entry.get("value"),
            "status": entry.get("status"),
            "available_at": None
            if entry.get("available_at") is None
            else str(entry.get("available_at")),
        }
        status = entry.get("status")
        if status != "VALID":
            if not item.get("missing_allowed"):
                schema_problems.append(f"{key} is {status}; the model needs it VALID")
            row[column] = None
            continue
        try:
            available = _parse_time(entry.get("available_at"))
        except ValueError:
            value_problems.append(f"{key} has an unreadable available_at")
            continue
        if available is not None and available > request.decision_at:
            value_problems.append(f"{key} became available after the decision time")
        raw = entry.get("value")
        if item["value_kind"] == "NUMBER":
            if isinstance(raw, bool) or not isinstance(raw, int | float | str):
                value_problems.append(f"{key} is not a number")
                continue
            try:
                number = float(raw)
            except ValueError:
                value_problems.append(f"{key} is not a number")
                continue
            if not math.isfinite(number):
                value_problems.append(f"{key} is not finite")
                continue
            row[column] = number
        else:
            if not isinstance(raw, str):
                value_problems.append(f"{key} is not a label")
                continue
            row[column] = raw
    if schema_problems:
        return PredictionValidity.REJECTED_SCHEMA, schema_problems + value_problems, row, snapshot
    if value_problems:
        return PredictionValidity.REJECTED_INVALID_VALUES, value_problems, row, snapshot
    return PredictionValidity.VALID, [], row, snapshot


def _frame(model: LoadedModel, row: Mapping[str, Any]) -> pl.DataFrame:
    data: dict[str, Any] = {}
    schema: dict[str, Any] = {}
    for column in model.preprocessor.numeric:
        data[column] = [row.get(column)]
        schema[column] = pl.Float64
    for column in model.preprocessor.categorical:
        data[column] = [row.get(column)]
        schema[column] = pl.String
    return pl.DataFrame(data, schema=schema)


def compute_output(model: LoadedModel, row: Mapping[str, Any]) -> dict[str, Any]:
    x = model.preprocessor.transform(_frame(model, row))
    if model.task is TaskKind.REGRESSION:
        estimate = float(np.asarray(model.model.predict(x), dtype=np.float64)[0])
        return {"output_kind": model.output_kind.value, "estimate": estimate}
    raw = float(np.asarray(model.model.predict_proba(x)[:, 1], dtype=np.float64)[0])
    out: dict[str, Any] = {"output_kind": model.output_kind.value, "raw_score": raw}
    if model.output_kind is OutputKind.CALIBRATED_PROBABILITY:
        calibrated = model.calibrator(np.array([raw], dtype=np.float64))
        out["probability"] = float(calibrated[0])
    else:
        out["note"] = "uncalibrated score: an ordering, not a probability"
    return out


class InferenceEngine:
    def __init__(
        self, session_factory: sessionmaker[Session], settings: MLSettings, clock: Clock
    ) -> None:
        self._sessions = session_factory
        self.settings = settings
        self.clock = clock
        self._cache: dict[tuple[uuid.UUID, str], LoadedModel] = {}

    def _load(self, model: MLModelVersionRecord, artifact: MLModelArtifactRecord) -> LoadedModel:
        key = (model.id, artifact.sha256)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = read_artifact(self.settings.artifact_root(), artifact.path, artifact.sha256)
        if payload.get("training_run_id") != str(model.training_run_id):
            raise ArtifactError("the artifact belongs to another training run")
        loaded = LoadedModel(
            version=model.id,
            sha256=artifact.sha256,
            model=payload["model"],
            preprocessor=FittedPreprocessor.from_description(payload["preprocessor"]),
            calibrator=payload["calibrator"],
            task=TaskKind(payload["task"]),
            output_kind=OutputKind(payload["output_kind"]),
            schema=list(payload["input_schema"]),
        )
        if sha256_json(loaded.schema) != model.input_schema_sha256:
            raise ArtifactError("the artifact's input schema differs from the registry's")
        self._cache[key] = loaded
        return loaded

    def approved_version(self, model_name: str) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            approved = lifecycle.approved_versions(session, model_name)
        if not approved:
            raise InferenceError(f"{model_name} has no APPROVED version")
        return approved[0]

    def predict(
        self, model_version_id: uuid.UUID, request: PredictionRequest, *, actor: str
    ) -> PredictionResult:
        now = self.clock.now()
        with session_scope(self._sessions) as session:
            model = session.get(MLModelVersionRecord, model_version_id)
            if model is None:
                raise InferenceError(f"model version {model_version_id} does not exist")
            artifact = session.get(MLModelArtifactRecord, model.artifact_id)
            if artifact is None:
                raise InferenceError("the model version has no artifact")
            status = lifecycle.current_status(session, model.id)
            session.expunge(model)
            session.expunge(artifact)
        problems: list[str] = []
        output: dict[str, Any] | None = None
        snapshot: dict[str, Any] = {}
        validity = PredictionValidity.VALID
        if status is not MODE_STATUS[request.mode]:
            validity = PredictionValidity.REJECTED_MODEL_STATUS
            problems.append(
                f"{request.mode.value} predictions need a model in status "
                f"{MODE_STATUS[request.mode].value}; this version is {status.value}"
            )
        if request.decision_at > now:
            validity = PredictionValidity.REJECTED_INVALID_VALUES
            problems.append("the decision time is in the future")
        loaded: LoadedModel | None = None
        if validity is PredictionValidity.VALID:
            try:
                loaded = self._load(model, artifact)
            except ArtifactError as exc:
                validity = PredictionValidity.REJECTED_ARTIFACT
                problems.append(str(exc))
        if loaded is not None:
            validity, found, row, snapshot = validate_inputs(loaded.schema, request)
            problems.extend(found)
            if validity is PredictionValidity.VALID:
                output = compute_output(loaded, row)
        if not snapshot:
            snapshot = {k: dict(v) for k, v in sorted(request.features.items())}
        snapshot = _json_safe(snapshot)
        available = [
            _parse_time(v.get("available_at"))
            for k, v in snapshot.items()
            if isinstance(v, dict) and v.get("status") == "VALID"
        ]
        newest = max((a for a in available if a is not None), default=None)
        record = MLPredictionRecord(
            id=uuid.uuid4(),
            model_version_id=model.id,
            model_status=status,
            mode=request.mode,
            asset_id=request.asset_id,
            timeframe=request.timeframe,
            direction=request.direction,
            strategy_id=request.strategy_id,
            decision_at=request.decision_at,
            predicted_at=now,
            feature_snapshot=snapshot,
            feature_snapshot_sha256=sha256_json(snapshot),
            inputs_available_at=newest if newest is not None and newest <= now else None,
            input_schema_sha256=model.input_schema_sha256,
            artifact_sha256=artifact.sha256,
            validity=validity,
            problems=problems,
            output_kind=model.output_kind,
            output=output if validity is PredictionValidity.VALID else None,
            requested_by=actor,
        )
        with session_scope(self._sessions) as session:
            session.add(record)
        return PredictionResult(
            record.id,
            model.id,
            validity,
            problems,
            model.output_kind,
            record.output,
        )

    def predict_at(
        self,
        model_version_id: uuid.UUID,
        *,
        asset_id: uuid.UUID,
        timeframe: str,
        direction: Direction,
        decision_at: datetime,
        calendar: ExchangeCalendar,
        strategy_id: str | None = None,
        mode: PredictionMode = PredictionMode.APPROVED,
        actor: str,
    ) -> PredictionResult:
        """Compute the model's features point in time (RECORDED: only what Atlas had stored
        by ``decision_at``) with the strategy feature pipeline, then predict."""
        with session_scope(self._sessions) as session:
            model = session.get(MLModelVersionRecord, model_version_id)
            if model is None:
                raise InferenceError(f"model version {model_version_id} does not exist")
            dataset = session.get(MLDatasetVersionRecord, model.dataset_version_id)
            if dataset is None:
                raise InferenceError("the model's dataset version is missing")
            spec = DatasetSpec.model_validate(dataset.spec)
            wanted = {
                item["keys"].get(timeframe)
                for item in model.input_schema
                if item["kind"] == "FEATURE"
            }
            requests = [
                FeatureRequest(f.name, timeframe, f.params, f.optional)
                for f in spec.features
                if timeframe in spec.timeframes
            ]
            resolved = [r for r in REGISTRY.resolve_all(requests) if r.key in wanted]
            series = BarSeries(
                provider=dataset.provider,
                feed=dataset.feed,
                adjustment=PriceAdjustment(dataset.adjustment),
                calendar=calendar,
                settle=timedelta(seconds=spec.settle_seconds),
            )
            inputs = compute_features(
                session,
                resolved,
                asset_id=asset_id,
                timeframe=timeframe,
                data_cutoff=decision_at,
                known_at=decision_at,
                basis=KnowledgeBasis.RECORDED,
                series=series,
            )
        features = {
            key: {
                "value": value.to_json()["value"],
                "status": value.status.value,
                "available_at": value.to_json()["available_at"],
            }
            for key, value in inputs.values.items()
        }
        return self.predict(
            model_version_id,
            PredictionRequest(
                timeframe=timeframe,
                direction=direction,
                decision_at=decision_at,
                features=features,
                asset_id=asset_id,
                strategy_id=strategy_id,
                mode=mode,
            ),
            actor=actor,
        )

    def replay(self, prediction_id: uuid.UUID) -> dict[str, Any]:
        """Recompute a stored VALID prediction from its stored inputs and artifact."""
        with session_scope(self._sessions) as session:
            stored = session.get(MLPredictionRecord, prediction_id)
            if stored is None:
                raise InferenceError(f"prediction {prediction_id} does not exist")
            model = session.get(MLModelVersionRecord, stored.model_version_id)
            artifact = (
                None if model is None else session.get(MLModelArtifactRecord, model.artifact_id)
            )
            if model is None or artifact is None:
                raise InferenceError("the prediction's model version or artifact is missing")
            session.expunge_all()
        if stored.validity is not PredictionValidity.VALID or stored.output is None:
            return {"prediction_id": str(prediction_id), "replayable": False, "reason": "not VALID"}
        loaded = self._load(model, artifact)
        features = {
            k: v for k, v in stored.feature_snapshot.items() if not k.startswith("context:")
        }
        context = {
            k.removeprefix("context:"): v
            for k, v in stored.feature_snapshot.items()
            if k.startswith("context:")
        }
        request = PredictionRequest(
            timeframe=stored.timeframe,
            direction=Direction(context.get("direction", stored.direction.value)),
            decision_at=stored.decision_at,
            features=features,
            asset_id=stored.asset_id,
            strategy_id=stored.strategy_id,
            mode=stored.mode,
        )
        validity, problems, row, _ = validate_inputs(loaded.schema, request)
        recomputed = compute_output(loaded, row) if validity is PredictionValidity.VALID else None
        return {
            "prediction_id": str(prediction_id),
            "replayable": True,
            "artifact_sha256": artifact.sha256,
            "stored_output": stored.output,
            "recomputed_output": recomputed,
            "identical": recomputed == stored.output,
            "problems": problems,
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


__all__ = [
    "InferenceEngine",
    "InferenceError",
    "LoadedModel",
    "PredictionRequest",
    "PredictionResult",
    "compute_output",
    "validate_inputs",
]

"""Dataset persistence and loading (Phase 5A).

* :func:`save_dataset` stores a built dataset as a new, immutable version (the dataset
  identity is created on first use). Observations are written in order with their row
  index; the version records the content hash of exactly what was written.
* :func:`load_frame` reads a version back as a Polars frame (one row per observation, one
  column per feature; non-VALID values are null) plus its metadata.
* :func:`verify_dataset` rebuilds a version from its stored specification and compares
  content hashes: the reproducibility check.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session

from app.asset_discovery.sessions import ExchangeCalendar
from app.database.models import (
    MLDatasetObservationRecord,
    MLDatasetRecord,
    MLDatasetVersionRecord,
)
from app.feature_engine.registry import FEATURE_REGISTRY_VERSION, ValueKind, sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.datasets.builder import BuiltDataset, build_dataset
from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.enums import LabelStatus

CHUNK = 2_000


class DatasetNotFoundError(LookupError):
    pass


def save_dataset(
    session: Session, built: BuiltDataset, *, actor: str, provider: str, feed: str
) -> MLDatasetVersionRecord:
    spec = built.spec
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"ml_dataset:{spec.name}"}
    )
    dataset = session.scalar(select(MLDatasetRecord).where(MLDatasetRecord.name == spec.name))
    if dataset is None:
        dataset = MLDatasetRecord(name=spec.name, description=spec.description, created_by=actor)
        session.add(dataset)
        session.flush()
    latest = session.scalar(
        select(func.max(MLDatasetVersionRecord.version)).where(
            MLDatasetVersionRecord.dataset_id == dataset.id
        )
    )
    rows = [o.to_json(i) for i, o in enumerate(built.observations)]
    matured = [o for o in built.observations if o.label.status is LabelStatus.MATURED]
    decisions = [o.decision_at for o in built.observations]
    version = MLDatasetVersionRecord(
        dataset_id=dataset.id,
        version=(latest or 0) + 1,
        spec=spec.stored(),
        spec_sha256=spec.sha256,
        feature_schema=[s.to_json() for s in built.feature_schema],
        feature_schema_sha256=built.schema_sha256(),
        feature_registry_version=FEATURE_REGISTRY_VERSION,
        target_kind=spec.target.kind,
        target_definition=built.target_definition,
        target_sha256=sha256_json(built.target_definition),
        source=spec.source,
        basis=KnowledgeBasis.RECONSTRUCTED,
        origin=built.origin,
        data_known_at=spec.data_known_at,
        provider=provider,
        feed=feed,
        adjustment=spec.adjustment.value,
        timeframes=list(spec.timeframes),
        asset_count=len({o.asset_id for o in built.observations}),
        observation_count=len(rows),
        matured_count=len(matured),
        decision_start=min(decisions, default=None),
        decision_end=max(decisions, default=None),
        statistics=built.statistics,
        warnings=built.warnings,
        content_sha256=built.content_sha256(),
        code_version=built.code,
        created_by=actor,
    )
    session.add(version)
    session.flush()
    records = [
        {
            "dataset_version_id": version.id,
            "row_index": row["row_index"],
            "asset_id": o.asset_id,
            "symbol": o.symbol,
            "timeframe": o.timeframe,
            "direction": o.direction,
            "decision_at": o.decision_at,
            "data_known_at": o.data_known_at,
            "source": o.source,
            "strategy_id": o.strategy_id,
            "strategy_version": o.strategy_version,
            "proposal_id": o.proposal_id,
            "horizon_periods": o.horizon_periods,
            "features": o.features,
            "features_available_at": o.features_available_at,
            "last_input_period_at": o.last_input_period_at,
            "segments": o.segments,
            "label_status": o.label.status,
            "label_value": None if o.label.value is None else Decimal(repr(o.label.value)),
            "label_class": o.label.cls,
            "label_end_at": o.label.end_at,
            "outcome": o.label.outcome,
        }
        for row, o in zip(rows, built.observations, strict=True)
    ]
    for start in range(0, len(records), CHUNK):
        session.execute(insert(MLDatasetObservationRecord), records[start : start + CHUNK])
    return version


@dataclass(frozen=True)
class DatasetFrame:
    version: MLDatasetVersionRecord
    frame: pl.DataFrame  # one row per observation, ordered by row_index
    numeric: list[str]  # feature columns holding numbers
    categorical: list[str]  # feature columns holding labels
    optional: set[str]

    @property
    def features(self) -> list[str]:
        return [*self.numeric, *self.categorical]


def get_version(session: Session, version_id: uuid.UUID) -> MLDatasetVersionRecord:
    found = session.get(MLDatasetVersionRecord, version_id)
    if found is None:
        raise DatasetNotFoundError(f"dataset version {version_id} does not exist")
    return found


def _value(raw: Any, kind: str) -> Any:
    if raw is None or not isinstance(raw, dict) or raw.get("status") != "VALID":
        return None
    value = raw.get("value")
    if value is None:
        return None
    return float(value) if kind == ValueKind.NUMBER.value else str(value)


def load_frame(session: Session, version_id: uuid.UUID) -> DatasetFrame:
    version = get_version(session, version_id)
    schema: Sequence[dict[str, Any]] = version.feature_schema
    o = MLDatasetObservationRecord
    records = session.execute(
        select(
            o.row_index,
            o.asset_id,
            o.symbol,
            o.timeframe,
            o.direction,
            o.decision_at,
            o.strategy_id,
            o.strategy_version,
            o.horizon_periods,
            o.features,
            o.features_available_at,
            o.segments,
            o.label_status,
            o.label_value,
            o.label_class,
            o.label_end_at,
            o.outcome,
        )
        .where(o.dataset_version_id == version_id)
        .order_by(o.row_index)
    ).all()
    columns: dict[str, list[Any]] = {
        name: []
        for name in (
            "row_index",
            "asset_id",
            "symbol",
            "timeframe",
            "direction",
            "decision_at",
            "strategy_id",
            "strategy_version",
            "horizon_periods",
            "features_available_at",
            "market_trend",
            "volatility",
            "label_status",
            "label_value",
            "label_class",
            "label_end_at",
            "net_return",
        )
    }
    feature_columns = {item["column"]: item["value_kind"] for item in schema}
    values: dict[str, list[Any]] = {c: [] for c in feature_columns}
    for r in records:
        columns["row_index"].append(r.row_index)
        columns["asset_id"].append(str(r.asset_id))
        columns["symbol"].append(r.symbol)
        columns["timeframe"].append(r.timeframe)
        columns["direction"].append(r.direction.value)
        columns["decision_at"].append(r.decision_at)
        columns["strategy_id"].append(r.strategy_id or "none")
        columns["strategy_version"].append(r.strategy_version)
        columns["horizon_periods"].append(r.horizon_periods)
        columns["features_available_at"].append(r.features_available_at)
        columns["market_trend"].append(r.segments.get("market_trend", "UNKNOWN"))
        columns["volatility"].append(r.segments.get("volatility", "UNKNOWN"))
        columns["label_status"].append(r.label_status.value)
        columns["label_value"].append(r.label_value)
        columns["label_class"].append(r.label_class)
        columns["label_end_at"].append(r.label_end_at)
        net = r.outcome.get("net_return")
        columns["net_return"].append(None if net is None else float(net))
        for column, kind in feature_columns.items():
            values[column].append(_value(r.features.get(column), kind))
    frame_schema: dict[str, Any] = {
        "row_index": pl.Int64,
        "asset_id": pl.String,
        "symbol": pl.String,
        "timeframe": pl.String,
        "direction": pl.String,
        "decision_at": pl.Datetime("us", "UTC"),
        "strategy_id": pl.String,
        "strategy_version": pl.String,
        "horizon_periods": pl.Int64,
        "features_available_at": pl.Datetime("us", "UTC"),
        "market_trend": pl.String,
        "volatility": pl.String,
        "label_status": pl.String,
        "label_value": pl.Float64,
        "label_class": pl.Int64,
        "label_end_at": pl.Datetime("us", "UTC"),
        "net_return": pl.Float64,
    }
    for column, kind in feature_columns.items():
        frame_schema[column] = pl.Float64 if kind == ValueKind.NUMBER.value else pl.String
    frame = pl.DataFrame({**columns, **values}, schema=frame_schema)
    numeric = [c for c, k in feature_columns.items() if k == ValueKind.NUMBER.value]
    categorical = [c for c, k in feature_columns.items() if k != ValueKind.NUMBER.value]
    optional = {item["column"] for item in schema if item.get("optional")}
    return DatasetFrame(version, frame, numeric, categorical, optional)


@dataclass(frozen=True)
class Verification:
    version_id: uuid.UUID
    stored_sha256: str
    rebuilt_sha256: str
    observations: int

    @property
    def reproduced(self) -> bool:
        return self.stored_sha256 == self.rebuilt_sha256


def verify_dataset(
    session: Session,
    version_id: uuid.UUID,
    *,
    calendar: ExchangeCalendar,
    now: datetime,
) -> Verification:
    version = get_version(session, version_id)
    spec = DatasetSpec.model_validate(version.spec)
    built = build_dataset(
        session, spec, provider=version.provider, feed=version.feed, calendar=calendar, now=now
    )
    return Verification(
        version_id=version.id,
        stored_sha256=version.content_sha256,
        rebuilt_sha256=built.content_sha256(),
        observations=len(built.observations),
    )


__all__ = [
    "DatasetFrame",
    "DatasetNotFoundError",
    "Verification",
    "get_version",
    "load_frame",
    "save_dataset",
    "verify_dataset",
]

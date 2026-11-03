"""Strategy catalog, versions and status (Phase 4A).

* :func:`sync_feature_definitions` registers the code's feature registry. A definition is
  never changed under an existing name and version: a mismatch is refused.
* :func:`sync_strategies` registers every discovered strategy. The first sync creates
  version 1 with the default parameters. When the code's logic version or metadata
  (description, timeframes, schedule, required features, ...) differs from the current
  version, a new version is created with the *same* configuration, or the strategy is
  reported as needing attention when that configuration no longer validates (parameters
  are never changed silently). When the code adds parameter fields (Phase 4B), the new
  version's configuration keeps every stored value and writes the new fields' defaults out,
  and the version's reason names them.
* :func:`create_version` validates new parameters and adds a version (idempotent: the same
  parameters as the current version return it unchanged).
* :func:`set_status` appends an enable/disable event. A strategy without logic cannot be
  enabled.

Versions, configurations and status events are append-only; every change is audited.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.exceptions import AtlasError
from app.database.models import (
    ActorType,
    FeatureDefinitionRecord,
    StrategyConfigurationRecord,
    StrategyRecord,
    StrategyStatusEventRecord,
    StrategyVersionRecord,
)
from app.database.repositories import AuditLogRepository
from app.feature_engine.registry import (
    FEATURE_REGISTRY_VERSION,
    REGISTRY,
    FeatureRegistry,
    sha256_json,
)
from app.strategies.base import Strategy, StrategyDefinitionError, StrategyParameters

SYSTEM_ACTOR = "atlas:strategy-catalog"


class FeatureDefinitionConflictError(AtlasError):
    """A registered feature definition differs from the code under the same version."""

    code = "feature_definition_conflict"


class StrategyStatusError(AtlasError):
    """A status change that is not allowed."""

    code = "strategy_status_conflict"
    http_status = 409


@dataclass
class SyncReport:
    features_registered: int = 0
    strategies_registered: int = 0
    versions_created: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def sync_feature_definitions(session: Session, registry: FeatureRegistry = REGISTRY) -> int:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('feature_definitions'))"))
    stored = {
        (row.name, row.version): row.definition_sha256
        for row in session.execute(
            select(
                FeatureDefinitionRecord.name,
                FeatureDefinitionRecord.version,
                FeatureDefinitionRecord.definition_sha256,
            )
        )
    }
    added = 0
    for definition in registry.definitions():
        identity = (definition.name, definition.version)
        sha = definition.definition_sha256
        if identity in stored:
            if stored[identity] != sha:
                raise FeatureDefinitionConflictError(
                    f"feature {definition.name} {definition.version} changed without a new "
                    "version; give the changed definition a new version"
                )
            continue
        d = definition.describe()
        session.add(
            FeatureDefinitionRecord(
                name=definition.name,
                version=definition.version,
                source=definition.source,
                description=definition.description,
                method=definition.method,
                parameters=d["parameters"],
                timeframes=list(definition.timeframes),
                required_data=list(definition.required_data),
                availability=definition.availability,
                value_kind=definition.value_kind,
                status=definition.status,
                window_rule=definition.window_rule,
                definition_sha256=sha,
                registry_version=FEATURE_REGISTRY_VERSION,
            )
        )
        added += 1
    session.flush()
    return added


def parameters_json(params: StrategyParameters) -> dict[str, Any]:
    return params.model_dump(mode="json")


def schema_sha256(strategy: Strategy) -> str:
    return sha256_json(strategy.parameters_schema())


def required_feature_keys(
    strategy: Strategy, params: StrategyParameters, registry: FeatureRegistry = REGISTRY
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for timeframe in params.timeframes:
        resolved = registry.resolve_all(strategy.required_features(params, timeframe))
        out[timeframe] = [{"key": f.key, "optional": f.optional} for f in resolved]
    return out


def _metadata(
    strategy: Strategy, params: StrategyParameters, registry: FeatureRegistry
) -> dict[str, Any]:
    return {
        "logic_version": strategy.logic_version,
        "name": strategy.name,
        "description": strategy.description,
        "implemented": strategy.implemented,
        "supported_asset_classes": [c.value for c in strategy.supported_asset_classes],
        "supported_timeframes": list(strategy.supported_timeframes),
        "schedule": strategy.schedule.describe(),
        "required_features": required_feature_keys(strategy, params, registry),
        "feature_registry_version": FEATURE_REGISTRY_VERSION,
    }


def _version_metadata(version: StrategyVersionRecord) -> dict[str, Any]:
    return {
        "logic_version": version.logic_version,
        "name": version.name,
        "description": version.description,
        "implemented": version.implemented,
        "supported_asset_classes": list(version.supported_asset_classes),
        "supported_timeframes": list(version.supported_timeframes),
        "schedule": version.schedule,
        "required_features": version.required_features,
        "feature_registry_version": version.feature_registry_version,
    }


def latest_version(session: Session, strategy_id: str) -> StrategyVersionRecord | None:
    return session.scalar(
        select(StrategyVersionRecord)
        .where(StrategyVersionRecord.strategy_id == strategy_id)
        .order_by(StrategyVersionRecord.version.desc())
        .limit(1)
    )


def configuration(session: Session, version: StrategyVersionRecord) -> StrategyConfigurationRecord:
    found = session.get(StrategyConfigurationRecord, version.configuration_id)
    if found is None:  # impossible with the foreign key; keeps the type exact
        raise StrategyDefinitionError(f"configuration of {version.strategy_id} is missing")
    return found


def current_status(session: Session, strategy_id: str) -> StrategyStatusEventRecord | None:
    return session.scalar(
        select(StrategyStatusEventRecord)
        .where(StrategyStatusEventRecord.strategy_id == strategy_id)
        .order_by(StrategyStatusEventRecord.id.desc())
        .limit(1)
    )


def is_enabled(session: Session, strategy_id: str) -> bool:
    status = current_status(session, strategy_id)
    return status is not None and status.enabled


def _lock(session: Session, strategy_id: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"strategy_catalog:{strategy_id}"},
    )


def _configuration(
    session: Session, strategy: Strategy, params: StrategyParameters, actor: str
) -> StrategyConfigurationRecord:
    payload = parameters_json(params)
    sha = sha256_json(payload)
    found = session.scalar(
        select(StrategyConfigurationRecord).where(
            StrategyConfigurationRecord.strategy_id == strategy.strategy_id,
            StrategyConfigurationRecord.parameters_sha256 == sha,
        )
    )
    if found is not None:
        return found
    record = StrategyConfigurationRecord(
        strategy_id=strategy.strategy_id,
        parameters=payload,
        parameters_sha256=sha,
        schema_sha256=schema_sha256(strategy),
        created_by=actor,
    )
    session.add(record)
    session.flush()
    return record


def _add_version(
    session: Session,
    strategy: Strategy,
    config: StrategyConfigurationRecord,
    metadata: Mapping[str, Any],
    *,
    reason: str,
    actor: str,
    actor_type: ActorType,
    previous: StrategyVersionRecord | None,
) -> StrategyVersionRecord:
    number = 1 if previous is None else previous.version + 1
    version = StrategyVersionRecord(
        strategy_id=strategy.strategy_id,
        version=number,
        logic_version=metadata["logic_version"],
        configuration_id=config.id,
        name=metadata["name"],
        description=metadata["description"],
        implemented=metadata["implemented"],
        supported_asset_classes=metadata["supported_asset_classes"],
        supported_timeframes=metadata["supported_timeframes"],
        schedule=metadata["schedule"],
        required_features=metadata["required_features"],
        feature_registry_version=metadata["feature_registry_version"],
        reason=reason,
        created_by=actor,
    )
    session.add(version)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=actor_type,
        actor_id=actor,
        action="strategy.version_created",
        entity_type="strategy",
        entity_id=strategy.strategy_id,
        reason=reason,
        previous_state=None
        if previous is None
        else {"version": previous.version, "configuration_id": str(previous.configuration_id)},
        new_state={
            "version": number,
            "logic_version": version.logic_version,
            "configuration_id": str(config.id),
            "parameters": config.parameters,
        },
    )
    return version


def sync_strategies(
    session: Session,
    strategies: Iterable[Strategy],
    registry: FeatureRegistry = REGISTRY,
) -> SyncReport:
    report = SyncReport()
    for strategy in strategies:
        sid = strategy.strategy_id
        _lock(session, sid)
        if session.get(StrategyRecord, sid) is None:
            session.add(StrategyRecord(id=sid, family=strategy.family))
            session.flush()
            report.strategies_registered += 1
        current = latest_version(session, sid)
        if current is None:
            params = strategy.default_parameters()
            config = _configuration(session, strategy, params, SYSTEM_ACTOR)
            _add_version(
                session,
                strategy,
                config,
                _metadata(strategy, params, registry),
                reason="initial registration with default parameters",
                actor=SYSTEM_ACTOR,
                actor_type=ActorType.SYSTEM,
                previous=None,
            )
            report.versions_created.append(f"{sid} v1")
            continue
        stored = configuration(session, current)
        try:
            params = strategy.parse_parameters(stored.parameters)
        except StrategyDefinitionError as exc:
            report.problems.append(
                f"{sid}: the current configuration no longer validates ({exc}); create a "
                "new version with valid parameters"
            )
            continue
        wanted = _metadata(strategy, params, registry)
        normalized = parameters_json(params)
        added = sorted(set(normalized) - set(stored.parameters))
        if wanted != _version_metadata(current) or added:
            changed = sorted(k for k, v in wanted.items() if _version_metadata(current)[k] != v)
            reason = f"code changed: {', '.join(changed) or 'parameter schema'}"
            config = stored
            if normalized != stored.parameters:
                # New parameter fields: every stored value is kept; the new fields' defaults
                # are written out so the configuration is complete and explicit.
                config = _configuration(session, strategy, params, SYSTEM_ACTOR)
                reason += f"; new parameters at their defaults: {', '.join(added) or 'none'}"
            version = _add_version(
                session,
                strategy,
                config,
                wanted,
                reason=reason,
                actor=SYSTEM_ACTOR,
                actor_type=ActorType.SYSTEM,
                previous=current,
            )
            report.versions_created.append(f"{sid} v{version.version}")
    return report


def sync_catalog(
    session: Session, strategies: Iterable[Strategy], registry: FeatureRegistry = REGISTRY
) -> SyncReport:
    report = sync_strategies(session, strategies, registry)
    report.features_registered = sync_feature_definitions(session, registry)
    return report


def create_version(
    session: Session,
    strategy: Strategy,
    raw_parameters: Mapping[str, Any],
    *,
    reason: str,
    actor: str,
    registry: FeatureRegistry = REGISTRY,
) -> tuple[StrategyVersionRecord, bool]:
    """Validate ``raw_parameters`` (a partial set changes only the fields it names; the
    others keep the current version's values) and add a version; ``(version, False)`` if
    nothing would change."""
    _lock(session, strategy.strategy_id)
    if session.get(StrategyRecord, strategy.strategy_id) is None:
        raise StrategyDefinitionError(f"{strategy.strategy_id} is not registered; sync first")
    current = latest_version(session, strategy.strategy_id)
    base = {} if current is None else dict(configuration(session, current).parameters)
    params = strategy.parse_parameters({**base, **raw_parameters})
    config = _configuration(session, strategy, params, actor)
    metadata = _metadata(strategy, params, registry)
    if (
        current is not None
        and current.configuration_id == config.id
        and _version_metadata(current) == metadata
    ):
        return current, False
    version = _add_version(
        session,
        strategy,
        config,
        metadata,
        reason=reason,
        actor=actor,
        actor_type=ActorType.USER,
        previous=current,
    )
    return version, True


def set_status(
    session: Session, strategy: Strategy, *, enabled: bool, reason: str, actor: str
) -> tuple[StrategyStatusEventRecord, bool]:
    _lock(session, strategy.strategy_id)
    if session.get(StrategyRecord, strategy.strategy_id) is None:
        raise StrategyDefinitionError(f"{strategy.strategy_id} is not registered; sync first")
    if enabled and not strategy.implemented:
        raise StrategyStatusError(
            f"{strategy.strategy_id} has no evaluation logic yet (Phase 4B) and cannot be enabled"
        )
    current = current_status(session, strategy.strategy_id)
    if current is not None and current.enabled == enabled:
        return current, False
    event = StrategyStatusEventRecord(
        strategy_id=strategy.strategy_id, enabled=enabled, reason=reason, actor=actor
    )
    session.add(event)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=ActorType.USER,
        actor_id=actor,
        action="strategy.enabled" if enabled else "strategy.disabled",
        entity_type="strategy",
        entity_id=strategy.strategy_id,
        reason=reason,
        previous_state={"enabled": current.enabled if current is not None else False},
        new_state={"enabled": enabled},
    )
    return event, True


def version_counts(session: Session, strategy_ids: Sequence[str]) -> dict[str, int]:
    rows = session.execute(
        select(StrategyVersionRecord.strategy_id, func.count())
        .where(StrategyVersionRecord.strategy_id.in_(list(strategy_ids)))
        .group_by(StrategyVersionRecord.strategy_id)
    )
    return {row[0]: int(row[1]) for row in rows}


__all__ = [
    "SYSTEM_ACTOR",
    "FeatureDefinitionConflictError",
    "StrategyStatusError",
    "SyncReport",
    "configuration",
    "create_version",
    "current_status",
    "is_enabled",
    "latest_version",
    "parameters_json",
    "required_feature_keys",
    "set_status",
    "sync_catalog",
    "sync_feature_definitions",
    "sync_strategies",
    "version_counts",
]

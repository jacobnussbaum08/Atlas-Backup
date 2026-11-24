"""Strategy Laboratory API (Phase 4A; strategy logic, signal states and conflicts 4B).

Reads: the catalog (with current parameters and signal-state counts), strategy details,
versions, configurations, status history, the feature registry, evaluations (with signal
state, checks, eligibility, errors and feature snapshots; filterable by signal state),
proposals (with levels and conflicts) and recorded proposal conflicts.
Writes (operator token, audited): a new parameter version, and enable/disable. A strategy
without evaluation logic cannot be enabled.

There is no endpoint that approves, sizes or submits anything: proposals are hypotheses.
No performance statistics are served because none exist.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.market_data import market_data, require_operator
from app.core.exceptions import (
    DependencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
)
from app.database.models import Asset, FeatureDefinitionRecord
from app.database.sessions import session_scope
from app.feature_engine.registry import FEATURE_REGISTRY_VERSION
from app.strategies import catalog, queries
from app.strategies.base import Strategy, StrategyDefinitionError
from app.strategies.enums import Direction, EvaluationStatus, ProposalStatus, SignalState
from app.strategies.runtime import StrategyContext

router = APIRouter(prefix="/strategies", tags=["strategies"])
_lock = threading.Lock()


def strategies(request: Request) -> StrategyContext:
    """The strategy context, built and synced (catalog registration) on first use."""
    state = request.app.state
    context: StrategyContext | None = getattr(state, "strategies", None)
    if context is None:
        factory = getattr(state, "strategies_factory", None)
        if factory is None:
            raise DependencyUnavailableError("the strategy laboratory is not configured")
        with _lock:
            context = getattr(state, "strategies", None)
            if context is None:
                market = market_data(request)
                context = factory(market.provider.provider_id, market.provider.feed)
                state.strategies = context
    if getattr(state, "strategies_sync", None) is None:
        with _lock, session_scope(context.session_factory) as session:
            state.strategies_sync = catalog.sync_catalog(session, context.registry)
    return context


Context = Annotated[StrategyContext, Depends(strategies)]
Limit = Annotated[int, Query(ge=1, le=200)]
Timeframe = Annotated[str, Query(pattern="^(1Day|1Week)$")]


# ------------------------------------------------------------------ models
class CurrentVersion(BaseModel):
    id: uuid.UUID
    version: int
    logic_version: str
    configuration_id: uuid.UUID
    reason: str
    created_at: datetime


class StrategySummary(BaseModel):
    strategy_id: str
    family: str
    name: str
    description: str
    implemented: bool
    implementation_note: str
    enabled: bool
    status_changed_at: datetime | None
    current_version: CurrentVersion | None
    version_count: int
    supported_asset_classes: list[str]
    supported_timeframes: list[str]
    configured_timeframes: list[str]
    schedule: dict[str, Any]
    required_features: dict[str, Any]
    evaluations: int
    last_evaluation_at: datetime | None
    proposals: int
    active_proposals: int
    parameters: dict[str, Any] | None
    signal_states: dict[str, int]


class CatalogResponse(BaseModel):
    strategies: list[StrategySummary]
    feature_registry_version: str
    sync_problems: list[str]
    as_of: datetime
    notes: list[str]


class StrategyDetail(BaseModel):
    strategy: StrategySummary
    parameters: dict[str, Any] | None
    parameters_schema: dict[str, Any]
    versions: list[queries.VersionView]
    status_history: list[queries.StatusEventView]


class FeatureDefinitionView(BaseModel):
    name: str
    version: str
    source: str
    description: str
    method: str
    parameters: list[dict[str, Any]]
    timeframes: list[str]
    required_data: list[str]
    availability: str
    value_kind: str
    status: str
    window_rule: str | None
    definition_sha256: str
    registered_at: datetime | None


class FeatureRegistryResponse(BaseModel):
    registry_version: str
    definitions: list[FeatureDefinitionView]
    notes: list[str]


class EvaluationsResponse(BaseModel):
    evaluations: list[queries.EvaluationView]
    as_of: datetime


class EvaluationDetail(BaseModel):
    evaluation: queries.EvaluationView
    errors: list[queries.ErrorView]
    snapshots: list[queries.SnapshotView]
    proposals: list[queries.ProposalView]


class ProposalsResponse(BaseModel):
    proposals: list[queries.ProposalView]
    as_of: datetime
    notes: list[str]


class ProposalDetail(BaseModel):
    proposal: queries.ProposalView
    events: list[queries.ProposalEventView]
    snapshots: list[queries.SnapshotView]
    conflicts: list[queries.ConflictView]
    notes: list[str]


class ConflictsResponse(BaseModel):
    conflicts: list[queries.ConflictView]
    as_of: datetime
    notes: list[str]


class VersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, Any] = Field(
        description="parameters to change; unspecified ones keep the current version's values"
    )
    reason: str = Field(min_length=3, max_length=500)


class VersionResult(BaseModel):
    created: bool
    version: queries.VersionView


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    reason: str = Field(min_length=3, max_length=500)


class StatusResult(BaseModel):
    strategy_id: str
    enabled: bool
    changed: bool
    status_history: list[queries.StatusEventView]


# ------------------------------------------------------------------ helpers
IMPLEMENTED = (
    "Evaluation logic implemented (Phase 4B). Proposals are analytical hypotheses; default "
    "parameters are not optimized and no performance is claimed."
)
CONFLICTS_NOTE = (
    "A conflict records two overlapping proposals for the same asset in opposite "
    "directions. Nothing is resolved here: competition between proposals belongs to the "
    "Phase 5 decision coordinator."
)
NOT_IMPLEMENTED = (
    "Interface, parameters and feature dependencies only; evaluation logic arrives in "
    "Phase 4B. It is never evaluated and cannot be enabled until then."
)


def _strategy(ctx: StrategyContext, strategy_id: str) -> Strategy:
    if strategy_id not in ctx.registry:
        raise NotFoundError(f"unknown strategy {strategy_id}")
    return ctx.registry.get(strategy_id)


def _summary(
    strategy: Strategy, session: Session, counts: dict[str, dict[str, Any]]
) -> StrategySummary:
    sid = strategy.strategy_id
    version = catalog.latest_version(session, sid)
    status = catalog.current_status(session, sid)
    configured: list[str] = []
    required: dict[str, Any] = {}
    parameters: dict[str, Any] | None = None
    if version is not None:
        parameters = dict(catalog.configuration(session, version).parameters)
        configured = list(parameters.get("timeframes", []))
        required = version.required_features
    c = counts.get(sid, {})
    return StrategySummary(
        strategy_id=sid,
        family=strategy.family.value,
        name=strategy.name,
        description=strategy.description,
        implemented=strategy.implemented,
        implementation_note=IMPLEMENTED if strategy.implemented else NOT_IMPLEMENTED,
        enabled=status is not None and status.enabled,
        status_changed_at=None if status is None else status.created_at,
        current_version=None
        if version is None
        else CurrentVersion(
            id=version.id,
            version=version.version,
            logic_version=version.logic_version,
            configuration_id=version.configuration_id,
            reason=version.reason,
            created_at=version.created_at,
        ),
        version_count=catalog.version_counts(session, [sid]).get(sid, 0),
        supported_asset_classes=[a.value for a in strategy.supported_asset_classes],
        supported_timeframes=list(strategy.supported_timeframes),
        configured_timeframes=configured,
        schedule=strategy.schedule.describe(),
        required_features=required,
        evaluations=c.get("evaluations", 0),
        last_evaluation_at=c.get("last_evaluation_at"),
        proposals=c.get("proposals", 0),
        active_proposals=c.get("active_proposals", 0),
        parameters=parameters,
        signal_states=c.get("signal_states", {}),
    )


def _asset_id(session: Session, symbol: str | None, asset_id: uuid.UUID | None) -> uuid.UUID | None:
    if asset_id is not None or symbol is None:
        return asset_id
    found = session.scalar(select(Asset.id).where(Asset.symbol == symbol.strip().upper()).limit(1))
    if found is None:
        raise NotFoundError(f"unknown symbol {symbol}")
    return found


# ------------------------------------------------------------------ catalog
@router.get("", response_model=CatalogResponse)
def list_strategies(request: Request, ctx: Context) -> CatalogResponse:
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        counts = queries.counts(session, now)
        items = [_summary(s, session, counts) for s in ctx.registry]
    sync = getattr(request.app.state, "strategies_sync", None)
    return CatalogResponse(
        strategies=items,
        feature_registry_version=FEATURE_REGISTRY_VERSION,
        sync_problems=[] if sync is None else list(sync.problems),
        as_of=now,
        notes=[queries.DISCLAIMER, queries.PARAMETERS_NOTE],
    )


@router.get("/features", response_model=FeatureRegistryResponse)
def feature_registry(ctx: Context) -> FeatureRegistryResponse:
    with session_scope(ctx.session_factory) as session:
        registered = {
            (r.name, r.version): r.registered_at
            for r in session.scalars(select(FeatureDefinitionRecord))
        }
    definitions = []
    for d in ctx.registry.features.definitions():
        described = d.describe()
        definitions.append(
            FeatureDefinitionView(
                **described,
                definition_sha256=d.definition_sha256,
                registered_at=registered.get((d.name, d.version)),
            )
        )
    return FeatureRegistryResponse(
        registry_version=FEATURE_REGISTRY_VERSION,
        definitions=definitions,
        notes=[
            "Bar features use completed, settled periods only and exactly the newest "
            "window of periods; missing periods make a value MISSING_INPUTS, never zero.",
            "News features are UNAVAILABLE unless news retrieval covered the whole window.",
        ],
    )


# ------------------------------------------------------------------ evaluations
@router.get("/evaluations", response_model=EvaluationsResponse)
def list_evaluations(
    ctx: Context,
    strategy_id: str | None = None,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    status: EvaluationStatus | None = None,
    signal_state: SignalState | None = None,
    timeframe: Timeframe | None = None,
    before: datetime | None = None,
    limit: Limit = 50,
) -> EvaluationsResponse:
    with session_scope(ctx.session_factory) as session:
        rows = queries.evaluations(
            session,
            strategy_id=strategy_id,
            asset_id=_asset_id(session, symbol, asset_id),
            status=status,
            signal_state=signal_state,
            timeframe=timeframe,
            before=before,
            limit=limit,
        )
    return EvaluationsResponse(evaluations=rows, as_of=ctx.clock.now())


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationDetail)
def get_evaluation(ctx: Context, evaluation_id: uuid.UUID) -> EvaluationDetail:
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        found = queries.evaluation(session, evaluation_id)
        if found is None:
            raise NotFoundError("unknown evaluation")
        return EvaluationDetail(
            evaluation=found,
            errors=queries.evaluation_errors(session, evaluation_id),
            snapshots=queries.evaluation_snapshots(session, evaluation_id),
            proposals=queries.proposals(session, now, evaluation_id=evaluation_id, limit=10),
        )


@router.get("/feature-snapshots/{snapshot_id}", response_model=queries.SnapshotView)
def get_snapshot(ctx: Context, snapshot_id: uuid.UUID) -> queries.SnapshotView:
    with session_scope(ctx.session_factory) as session:
        found = queries.snapshot(session, snapshot_id)
    if found is None:
        raise NotFoundError("unknown feature snapshot")
    return found


# ------------------------------------------------------------------ proposals
@router.get("/proposals", response_model=ProposalsResponse)
def list_proposals(
    ctx: Context,
    strategy_id: str | None = None,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    status: ProposalStatus | None = None,
    direction: Direction | None = None,
    timeframe: Timeframe | None = None,
    before: datetime | None = None,
    limit: Limit = 50,
) -> ProposalsResponse:
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        rows = queries.proposals(
            session,
            now,
            strategy_id=strategy_id,
            asset_id=_asset_id(session, symbol, asset_id),
            status=status,
            direction=direction,
            timeframe=timeframe,
            before=before,
            limit=limit,
        )
    return ProposalsResponse(proposals=rows, as_of=now, notes=[queries.DISCLAIMER])


@router.get("/proposals/{proposal_id}", response_model=ProposalDetail)
def get_proposal(ctx: Context, proposal_id: uuid.UUID) -> ProposalDetail:
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        found = queries.proposal(session, proposal_id, now)
        if found is None:
            raise NotFoundError("unknown proposal")
        snapshots = [
            s
            for sid in found.feature_snapshot_ids
            if (s := queries.snapshot(session, sid)) is not None
        ]
        return ProposalDetail(
            proposal=found,
            events=queries.proposal_events(session, proposal_id),
            snapshots=snapshots,
            conflicts=queries.conflicts(session, now, proposal_id=proposal_id, limit=200),
            notes=[queries.DISCLAIMER, CONFLICTS_NOTE],
        )


@router.get("/conflicts", response_model=ConflictsResponse)
def list_conflicts(
    ctx: Context,
    strategy_id: str | None = None,
    symbol: str | None = None,
    asset_id: uuid.UUID | None = None,
    limit: Limit = 50,
) -> ConflictsResponse:
    """Recorded opposite-direction conflicts between proposals (newest first)."""
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        rows = queries.conflicts(
            session,
            now,
            asset_id=_asset_id(session, symbol, asset_id),
            strategy_id=strategy_id,
            limit=limit,
        )
    return ConflictsResponse(conflicts=rows, as_of=now, notes=[CONFLICTS_NOTE])


# ------------------------------------------------------------------ one strategy
@router.get("/{strategy_id}", response_model=StrategyDetail)
def get_strategy(ctx: Context, strategy_id: str) -> StrategyDetail:
    strategy = _strategy(ctx, strategy_id)
    now = ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        counts = queries.counts(session, now)
        summary = _summary(strategy, session, counts)
        versions = queries.versions(session, strategy_id)
        return StrategyDetail(
            strategy=summary,
            parameters=versions[0].parameters if versions else None,
            parameters_schema=strategy.parameters_schema(),
            versions=versions,
            status_history=queries.status_history(session, strategy_id),
        )


@router.get("/{strategy_id}/versions", response_model=list[queries.VersionView])
def list_versions(ctx: Context, strategy_id: str) -> list[queries.VersionView]:
    _strategy(ctx, strategy_id)
    with session_scope(ctx.session_factory) as session:
        return queries.versions(session, strategy_id)


@router.get("/{strategy_id}/configurations", response_model=list[queries.ConfigurationView])
def list_configurations(ctx: Context, strategy_id: str) -> list[queries.ConfigurationView]:
    _strategy(ctx, strategy_id)
    with session_scope(ctx.session_factory) as session:
        return queries.configurations(session, strategy_id)


@router.post(
    "/{strategy_id}/versions",
    response_model=VersionResult,
    dependencies=[Depends(require_operator)],
)
def create_version(ctx: Context, strategy_id: str, body: VersionBody) -> VersionResult:
    """Validate parameters and add an immutable version (operator token). Identical
    parameters return the current version with ``created: false``."""
    strategy = _strategy(ctx, strategy_id)
    try:
        with session_scope(ctx.session_factory) as session:
            version, created = catalog.create_version(
                session, strategy, body.parameters, reason=body.reason, actor="operator:api"
            )
            view = queries.version_view(version, catalog.configuration(session, version))
    except StrategyDefinitionError as exc:
        raise InvalidRequestError(str(exc)) from exc
    return VersionResult(created=created, version=view)


@router.post(
    "/{strategy_id}/status",
    response_model=StatusResult,
    dependencies=[Depends(require_operator)],
)
def set_status(ctx: Context, strategy_id: str, body: StatusBody) -> StatusResult:
    """Enable or disable a strategy (operator token). Strategies without evaluation logic
    cannot be enabled (409)."""
    strategy = _strategy(ctx, strategy_id)
    with session_scope(ctx.session_factory) as session:
        event, changed = catalog.set_status(
            session, strategy, enabled=body.enabled, reason=body.reason, actor="operator:api"
        )
        enabled = event.enabled
        history = queries.status_history(session, strategy_id)
    return StatusResult(
        strategy_id=strategy_id, enabled=enabled, changed=changed, status_history=history
    )


__all__ = ["router", "strategies"]

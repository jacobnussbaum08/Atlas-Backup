"""Asset universe API (Phase 3A): instruments, their qualification and their history.

Read-only. Layers: the discovery pool (every active instrument a supported provider
directory lists), the qualified universe (instruments passing the active rule set) and the
active watchlist (qualified instruments ranked for analytical attention). Nothing here
trades, sizes or approves anything; watchlist scores are analytical priority only.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.market_data import QueryTime, market_data
from app.asset_discovery import membership, queries
from app.asset_discovery.enums import (
    ClassificationSource,
    DiscoveryJobKind,
    InstrumentType,
    LifecycleEventType,
    MembershipAction,
    MetricStatus,
    ProviderAssetStatus,
    QualificationOutcome,
    ReferenceSource,
    UniverseLayer,
    ValidFromBasis,
)
from app.asset_discovery.runtime import AssetDiscoveryContext, build_asset_discovery_context
from app.asset_discovery.scoring import DISCLAIMER
from app.core.exceptions import DependencyUnavailableError, NotFoundError
from app.data_engine.runtime import MarketDataContext
from app.database.models import (
    Asset,
    AssetClass,
    AssetIdentifier,
    AssetLifecycleEventRecord,
    AssetMetricValueRecord,
    AssetReferenceVersionRecord,
    AssetStatus,
    MappingStatus,
    ProviderAssetRecord,
    QualificationEvaluationRecord,
    QualificationRuleSetRecord,
    UniverseMembershipEventRecord,
    WatchlistScoreRecord,
)
from app.database.sessions import session_scope

router = APIRouter(prefix="/assets", tags=["assets"])
_lock = threading.Lock()


def asset_discovery(
    request: Request, market: Annotated[MarketDataContext, Depends(market_data)]
) -> AssetDiscoveryContext:
    state = request.app.state
    context: AssetDiscoveryContext | None = getattr(state, "asset_discovery", None)
    if context is None:
        with _lock:
            context = getattr(state, "asset_discovery", None)
            if context is None:
                if market is None:
                    raise DependencyUnavailableError("asset discovery is not configured")
                context = build_asset_discovery_context(market, state.clock)
                state.asset_discovery = context
    return context


Context = Annotated[AssetDiscoveryContext, Depends(asset_discovery)]


# ------------------------------------------------------------------ models
class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
    notes: list[str] = Field(default_factory=list)


class AssetSummary(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    name: str | None
    asset_class: AssetClass
    instrument_type: InstrumentType
    exchange: str | None
    provider_exchange: str | None
    status: AssetStatus
    provider_asset_id: str | None
    provider_symbol: str | None
    tradable: bool | None
    shortable: bool | None
    easy_to_borrow: bool | None
    marginable: bool | None
    fractionable: bool | None
    attributes: list[str]
    first_discovered_at: datetime | None
    last_verified_at: datetime | None
    in_discovery_pool: bool
    qualified: bool
    qualified_since: datetime | None
    on_watchlist: bool
    qualification_outcome: QualificationOutcome | None
    failed_rules: list[str]
    unavailable_rules: list[str]
    watchlist_rank: int | None
    watchlist_score: Decimal | None


class ReferenceVersion(BaseModel):
    id: int
    version: int
    observed_at: datetime
    source: ReferenceSource
    discovery_job_id: uuid.UUID | None
    symbol: str
    provider_symbol: str
    name: str | None
    provider_asset_class: str
    provider_exchange: str | None
    exchange_mic: str | None
    status: ProviderAssetStatus
    tradable: bool | None
    marginable: bool | None
    shortable: bool | None
    easy_to_borrow: bool | None
    borrow_status: str | None
    fractionable: bool | None
    margin_requirement_long: Decimal | None
    margin_requirement_short: Decimal | None
    min_order_size: Decimal | None
    min_trade_increment: Decimal | None
    price_increment: Decimal | None
    attributes: list[str]
    delisting_evidence: str | None
    content_sha256: str


class ProviderIdentity(BaseModel):
    provider: str
    provider_asset_id: str
    provider_asset_class: str
    provider_status: ProviderAssetStatus
    current_symbol: str
    current_version: int
    first_seen_at: datetime
    last_seen_at: datetime
    missing_since: datetime | None


class Mapping(BaseModel):
    provider: str
    provider_symbol: str
    provider_exchange: str | None
    status: MappingStatus
    valid_from: datetime
    valid_to: datetime | None
    valid_from_basis: ValidFromBasis


class LayerState(BaseModel):
    layer: UniverseLayer
    member: bool
    since: datetime | None
    reason_code: str | None
    reason: str | None


class RuleResultModel(BaseModel):
    rule: str
    category: str
    mandatory: bool
    status: str
    message: str
    value: Any = None
    threshold: Any = None


class EvaluationModel(BaseModel):
    evaluation_id: uuid.UUID
    job_id: uuid.UUID
    as_of: datetime
    outcome: QualificationOutcome
    rule_set_id: uuid.UUID
    rule_set_code: str | None = None
    rule_set_version: int | None = None
    failed_rules: list[str]
    unavailable_rules: list[str]
    rule_results: list[RuleResultModel]
    short_eligibility: dict[str, Any]
    reference_version_id: int | None


class MetricModel(BaseModel):
    metric: str
    window: str
    timeframe: str
    provider: str
    feed: str
    feature_version: str
    as_of: datetime
    source_start: datetime | None
    source_end: datetime | None
    observations: int
    expected_observations: int
    value: Decimal | None
    status: MetricStatus
    details: dict[str, Any]


class ScoreModel(BaseModel):
    job_id: uuid.UUID
    asset_id: uuid.UUID
    symbol: str | None = None
    as_of: datetime
    rank: int | None
    overall_score: Decimal | None
    weight_coverage: Decimal
    selected: bool
    factor_scores: dict[str, Any]
    explanation: str
    scoring_version_id: uuid.UUID


class AssetDetail(BaseModel):
    asset: AssetSummary
    classification_source: ClassificationSource
    country: str | None
    quote_currency: str
    metadata_source: str | None
    providers: list[ProviderIdentity]
    reference: ReferenceVersion | None
    reference_versions: int
    mappings: list[Mapping]
    layers: list[LayerState]
    latest_evaluation: EvaluationModel | None
    latest_score: ScoreModel | None
    notes: list[str]


class QualificationResponse(BaseModel):
    asset_id: uuid.UUID
    evaluations: list[EvaluationModel]
    metrics: list[MetricModel]
    notes: list[str]


class LifecycleEvent(BaseModel):
    id: int
    event_type: LifecycleEventType
    effective_at: datetime
    effective_at_basis: str
    observed_at: datetime
    provider: str | None
    previous_state: dict[str, Any] | None
    new_state: dict[str, Any] | None
    reason: str
    discovery_job_id: uuid.UUID | None
    reference_version_id: int | None


class MembershipEvent(BaseModel):
    id: int
    layer: UniverseLayer
    action: MembershipAction
    sequence: int
    effective_at: datetime
    recorded_at: datetime
    reason_code: str
    reason: str
    job_id: uuid.UUID | None
    evaluation_id: uuid.UUID | None
    score_id: int | None


class HistoryResponse(BaseModel):
    asset_id: uuid.UUID
    lifecycle: list[LifecycleEvent]
    membership: list[MembershipEvent]
    reference_versions: list[ReferenceVersion]
    mappings: list[Mapping]
    notes: list[str]


# ------------------------------------------------------------------ helpers
def _evaluation(session: Any, e: QualificationEvaluationRecord) -> EvaluationModel:
    rule_set = session.get(QualificationRuleSetRecord, e.rule_set_id)
    return EvaluationModel(
        evaluation_id=e.id,
        job_id=e.job_id,
        as_of=e.as_of,
        outcome=e.outcome,
        rule_set_id=e.rule_set_id,
        rule_set_code=None if rule_set is None else rule_set.code,
        rule_set_version=None if rule_set is None else rule_set.version,
        failed_rules=list(e.failed_rules),
        unavailable_rules=list(e.unavailable_rules),
        rule_results=[RuleResultModel.model_validate(r) for r in e.rule_results],
        short_eligibility=e.short_eligibility,
        reference_version_id=e.reference_version_id,
    )


def score_model(s: WatchlistScoreRecord, symbol: str | None = None) -> ScoreModel:
    return ScoreModel(
        job_id=s.job_id,
        asset_id=s.asset_id,
        symbol=symbol,
        as_of=s.as_of,
        rank=s.rank,
        overall_score=s.overall_score,
        weight_coverage=s.weight_coverage,
        selected=s.selected,
        factor_scores=s.factor_scores,
        explanation=s.explanation,
        scoring_version_id=s.scoring_version_id,
    )


SortParam = Annotated[queries.SortKey, Query()]
_LIST_NOTES = [
    "Instrument type: Alpaca's directory reports every US listing as us_equity, so ETFs, "
    "preferreds, warrants, units and rights are UNCLASSIFIED unless an operator classified "
    "them.",
    "Tradability, shortability and borrow status are provider reference data as of "
    "last_verified_at; borrow must be revalidated before any short is proposed.",
]


def _list(
    ctx: AssetDiscoveryContext,
    layer: UniverseLayer | None,
    *,
    search: str | None,
    status: list[AssetStatus] | None,
    exchange: list[str] | None,
    asset_class: list[AssetClass] | None,
    instrument_type: list[InstrumentType] | None,
    outcome: QualificationOutcome | None,
    tradable: bool | None,
    shortable: bool | None,
    sort: queries.SortKey,
    order: str,
    limit: int,
    offset: int,
    notes: list[str],
) -> Page[AssetSummary]:
    flt = queries.AssetFilter(
        search=search,
        layer=layer,
        statuses=status or (),
        exchanges=[e.upper() for e in exchange or ()],
        asset_classes=[c.value for c in asset_class or ()],
        instrument_types=[t.value for t in instrument_type or ()],
        outcome=outcome,
        tradable=tradable,
        shortable=shortable,
    )
    with session_scope(ctx.session_factory) as session:
        total, rows = queries.asset_rows(
            session,
            ctx.provider.provider_id,
            flt,
            sort=sort,
            descending=order == "desc",
            limit=limit,
            offset=offset,
        )
    return Page[AssetSummary](
        items=[AssetSummary(**r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        notes=notes,
    )


def _list_params(
    search: Annotated[
        str | None, Query(max_length=64, description="Symbol or name contains")
    ] = None,
    status: Annotated[list[AssetStatus] | None, Query()] = None,
    exchange: Annotated[list[str] | None, Query(description="MIC, e.g. XNAS")] = None,
    asset_class: Annotated[list[AssetClass] | None, Query()] = None,
    instrument_type: Annotated[list[InstrumentType] | None, Query()] = None,
    outcome: Annotated[QualificationOutcome | None, Query(description="Latest evaluation")] = None,
    tradable: bool | None = None,
    shortable: bool | None = None,
    sort: SortParam = "symbol",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return {
        "search": search,
        "status": status,
        "exchange": exchange,
        "asset_class": asset_class,
        "instrument_type": instrument_type,
        "outcome": outcome,
        "tradable": tradable,
        "shortable": shortable,
        "sort": sort,
        "order": order,
        "limit": limit,
        "offset": offset,
    }


ListParams = Annotated[dict[str, Any], Depends(_list_params)]


# ------------------------------------------------------------------ list endpoints
@router.get("", response_model=Page[AssetSummary])
def list_assets(
    ctx: Context, params: ListParams, layer: UniverseLayer | None = None
) -> Page[AssetSummary]:
    """Every catalogued instrument (active, inactive and delisted), filterable by layer."""
    return _list(ctx, layer, notes=_LIST_NOTES, **params)


@router.get("/discovery-pool", response_model=Page[AssetSummary])
def discovery_pool(ctx: Context, params: ListParams) -> Page[AssetSummary]:
    """Active instruments listed by supported provider directories: a catalog, not a
    statement of eligibility."""
    return _list(ctx, UniverseLayer.DISCOVERY_POOL, notes=_LIST_NOTES, **params)


@router.get("/qualified", response_model=Page[AssetSummary])
def qualified(ctx: Context, params: ListParams) -> Page[AssetSummary]:
    """Instruments currently passing every mandatory rule of the active rule set."""
    return _list(ctx, UniverseLayer.QUALIFIED_UNIVERSE, notes=_LIST_NOTES, **params)


@router.get("/watchlist", response_model=Page[AssetSummary])
def watchlist(ctx: Context, params: ListParams) -> Page[AssetSummary]:
    """Qualified instruments ranked for analytical attention (default sort: rank)."""
    if params["sort"] == "symbol":
        params = {**params, "sort": "rank"}
    return _list(ctx, UniverseLayer.ACTIVE_WATCHLIST, notes=[DISCLAIMER, *_LIST_NOTES], **params)


# ------------------------------------------------------------------ one asset
def _asset(session: Session, asset_id: uuid.UUID) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise NotFoundError(f"asset {asset_id} not found")
    return asset


def _mappings(session: Any, asset_id: uuid.UUID) -> list[Mapping]:
    rows = session.scalars(
        select(AssetIdentifier)
        .where(AssetIdentifier.asset_id == asset_id)
        .order_by(AssetIdentifier.provider, AssetIdentifier.valid_from)
    )
    return [Mapping.model_validate(m, from_attributes=True) for m in rows]


@router.get("/{asset_id}", response_model=AssetDetail)
def asset_detail(asset_id: uuid.UUID, ctx: Context) -> AssetDetail:
    provider = ctx.provider.provider_id
    with session_scope(ctx.session_factory) as session:
        asset = _asset(session, asset_id)
        summary = _summary_for(session, provider, asset)
        identities = session.scalars(
            select(ProviderAssetRecord).where(ProviderAssetRecord.asset_id == asset_id)
        ).all()
        current = next((p for p in identities if p.provider == provider), None)
        reference = None
        versions = 0
        if current is not None:
            record = session.scalars(
                select(AssetReferenceVersionRecord).where(
                    AssetReferenceVersionRecord.provider_asset_ref == current.id,
                    AssetReferenceVersionRecord.version == current.current_version,
                )
            ).one_or_none()
            reference = (
                None
                if record is None
                else ReferenceVersion.model_validate(record, from_attributes=True)
            )
            versions = current.current_version
        states = []
        for layer in UniverseLayer:
            state = membership.current_states(session, layer, [asset_id]).get(asset_id)
            states.append(
                LayerState(
                    layer=layer,
                    member=bool(state and state.member),
                    since=None if state is None else state.since,
                    reason_code=None if state is None else state.reason_code,
                    reason=None if state is None else state.reason,
                )
            )
        qual_job = queries.latest_job_id(session, DiscoveryJobKind.QUALIFICATION, provider)
        evaluation = None
        if qual_job is not None:
            found = session.scalars(
                select(QualificationEvaluationRecord).where(
                    QualificationEvaluationRecord.job_id == qual_job,
                    QualificationEvaluationRecord.asset_id == asset_id,
                )
            ).one_or_none()
            evaluation = None if found is None else _evaluation(session, found)
        score_job = queries.latest_job_id(session, DiscoveryJobKind.WATCHLIST_SCORING, provider)
        score = None
        if score_job is not None:
            found_score = session.scalars(
                select(WatchlistScoreRecord).where(
                    WatchlistScoreRecord.job_id == score_job,
                    WatchlistScoreRecord.asset_id == asset_id,
                )
            ).one_or_none()
            score = None if found_score is None else score_model(found_score, asset.symbol)
        return AssetDetail(
            asset=summary,
            classification_source=asset.classification_source,
            country=asset.country,
            quote_currency=asset.quote_currency,
            metadata_source=asset.metadata_source,
            providers=[
                ProviderIdentity.model_validate(p, from_attributes=True) for p in identities
            ],
            reference=reference,
            reference_versions=versions,
            mappings=_mappings(session, asset_id),
            layers=states,
            latest_evaluation=evaluation,
            latest_score=score,
            notes=[
                "Country is not reported by the provider and is left empty.",
                "Lifecycle dates are when Atlas observed a change; the provider reports none.",
                *(_LIST_NOTES[:1] if asset.instrument_type is InstrumentType.UNCLASSIFIED else []),
            ],
        )


def _summary_for(session: Any, provider: str, asset: Asset) -> AssetSummary:
    _, rows = queries.asset_rows(
        session,
        provider,
        queries.AssetFilter(search=asset.symbol),
        sort="symbol",
        descending=False,
        limit=500,
        offset=0,
    )
    row = next((r for r in rows if r["asset_id"] == asset.id), None)
    if row is None:  # pragma: no cover - the asset exists, so the query finds it
        raise NotFoundError(f"asset {asset.id} not found")
    return AssetSummary(**row)


@router.get("/{asset_id}/qualification", response_model=QualificationResponse)
def asset_qualification(
    asset_id: uuid.UUID,
    ctx: Context,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    as_of: QueryTime | None = None,
) -> QualificationResponse:
    """Evaluations newest first (``as_of``: only those evaluated at or before it), with
    the measurements behind the newest one."""
    with session_scope(ctx.session_factory) as session:
        _asset(session, asset_id)
        stmt = select(QualificationEvaluationRecord).where(
            QualificationEvaluationRecord.asset_id == asset_id
        )
        if as_of is not None:
            stmt = stmt.where(QualificationEvaluationRecord.as_of <= as_of)
        evaluations = session.scalars(
            stmt.order_by(QualificationEvaluationRecord.as_of.desc()).limit(limit)
        ).all()
        metrics: list[MetricModel] = []
        if evaluations:
            metrics = [
                MetricModel.model_validate(m, from_attributes=True)
                for m in session.scalars(
                    select(AssetMetricValueRecord)
                    .where(
                        AssetMetricValueRecord.asset_id == asset_id,
                        AssetMetricValueRecord.job_id == evaluations[0].job_id,
                    )
                    .order_by(AssetMetricValueRecord.metric)
                )
            ]
        return QualificationResponse(
            asset_id=asset_id,
            evaluations=[_evaluation(session, e) for e in evaluations],
            metrics=metrics,
            notes=[
                "UNAVAILABLE means the input does not exist yet (e.g. no daily history); it is "
                "never counted as a failure. Optional rules apply only where data exists.",
                "Volumes on a single-venue feed (IEX) are that venue's volume only.",
                "Thresholds are configurable initial defaults, not empirically validated.",
            ],
        )


@router.get("/{asset_id}/history", response_model=HistoryResponse)
def asset_history(asset_id: uuid.UUID, ctx: Context) -> HistoryResponse:
    """Lifecycle events, membership events, reference versions and mappings."""
    with session_scope(ctx.session_factory) as session:
        _asset(session, asset_id)
        lifecycle = session.scalars(
            select(AssetLifecycleEventRecord)
            .where(AssetLifecycleEventRecord.asset_id == asset_id)
            .order_by(AssetLifecycleEventRecord.effective_at, AssetLifecycleEventRecord.id)
        ).all()
        events = session.scalars(
            select(UniverseMembershipEventRecord)
            .where(UniverseMembershipEventRecord.asset_id == asset_id)
            .order_by(UniverseMembershipEventRecord.effective_at, UniverseMembershipEventRecord.id)
        ).all()
        versions = session.scalars(
            select(AssetReferenceVersionRecord)
            .where(AssetReferenceVersionRecord.asset_id == asset_id)
            .order_by(AssetReferenceVersionRecord.observed_at, AssetReferenceVersionRecord.version)
        ).all()
        return HistoryResponse(
            asset_id=asset_id,
            lifecycle=[LifecycleEvent.model_validate(e, from_attributes=True) for e in lifecycle],
            membership=[MembershipEvent.model_validate(e, from_attributes=True) for e in events],
            reference_versions=[
                ReferenceVersion.model_validate(v, from_attributes=True) for v in versions
            ],
            mappings=_mappings(session, asset_id),
            notes=[
                "All history is append-only. Membership at a past time T is the newest event "
                "effective at or before T that was recorded by then.",
                "effective_at of lifecycle events is the observation time (basis OBSERVED).",
            ],
        )


__all__ = ["Page", "asset_discovery", "router", "score_model"]

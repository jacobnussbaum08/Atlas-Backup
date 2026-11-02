"""Market intelligence API (Phase 3B).

Read endpoints describe market conditions as Atlas recorded them; ``at`` (default now)
returns what was known then. Starting a run is an operational action (operator token, as
for discovery). Nothing here produces signals, sizes positions or places orders: market
intelligence describes conditions and authorizes nothing.

Numbers are decimal strings, as stored. Probabilities are normalized evidence weights, not
calibrated likelihoods (``app.market_intelligence.classification``).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.v1.market_data import QueryTime, market_data, require_operator
from app.core.exceptions import DependencyUnavailableError, InvalidRequestError, NotFoundError
from app.data_engine.runtime import MarketDataContext
from app.database.models import IntelligenceRunRecord
from app.database.sessions import session_scope
from app.market_intelligence import queries
from app.market_intelligence.enums import KnowledgeBasis, RunKind, RunStatus
from app.market_intelligence.runtime import (
    IntelligenceContext,
    build_intelligence_context,
    execute_blocking,
)
from app.market_intelligence.service import resolve_config

router = APIRouter(prefix="/intelligence", tags=["market-intelligence"])
_lock = threading.Lock()

DISCLAIMER = (
    "Market intelligence describes measured conditions. Probabilities are normalized evidence "
    "weights from configured formulas, not calibrated likelihoods or forecasts; nothing here "
    "is a trading signal."
)
TIMEFRAMES = ("1Day", "1Week")


def intelligence(
    request: Request, market: Annotated[MarketDataContext, Depends(market_data)]
) -> IntelligenceContext:
    state = request.app.state
    context: IntelligenceContext | None = getattr(state, "market_intelligence", None)
    if context is None:
        with _lock:
            context = getattr(state, "market_intelligence", None)
            if context is None:
                if market is None:
                    raise DependencyUnavailableError("market intelligence is not configured")
                context = build_intelligence_context(market, state.clock)
                state.market_intelligence = context
    return context


Context = Annotated[IntelligenceContext, Depends(intelligence)]
Timeframe = Annotated[Literal["1Day", "1Week"], Query(description="1Day or 1Week")]


# ------------------------------------------------------------------ models
class Run(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: RunKind
    status: RunStatus
    basis: KnowledgeBasis
    provider: str
    feed: str
    adjustment: str
    as_of_start: datetime
    as_of_end: datetime
    requested_by: str
    params: dict[str, Any]
    summary: dict[str, Any]
    error: str | None
    started_at: datetime
    heartbeat_at: datetime
    completed_at: datetime | None


class ClassificationView(BaseModel):
    dimension: str
    timeframe: str
    label: str
    confidence: str
    probabilities: dict[str, str | None]
    quality: str
    algorithm: str
    as_of: datetime
    basis: KnowledgeBasis
    available_at: datetime
    inputs_available_at: datetime | None
    evidence: dict[str, Any]
    reasons: list[str]


class Participation(BaseModel):
    status: str
    members_total: int
    members_evaluated: int
    metrics: dict[str, Any]
    cross_asset: dict[str, Any]
    limitations: list[str]


class MarketConditions(BaseModel):
    available: bool
    run: Run | None
    as_of: datetime | None
    benchmark_symbol: str | None
    classifications: list[ClassificationView]
    participation: Participation | None
    notes: list[str]


class RegimePoint(BaseModel):
    as_of: datetime
    basis: KnowledgeBasis
    trend: ClassificationView | None = None
    volatility: ClassificationView | None = None
    stability: ClassificationView | None = None


class RegimeHistory(BaseModel):
    timeframe: str
    points: list[RegimePoint]
    notes: list[str]


class AssetIntelligence(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    timeframe: str
    quality: str
    as_of: datetime
    last_event_at: datetime | None
    bars_used: int
    expected_bars: int
    features: dict[str, str | None]
    trend: ClassificationView | None
    volatility: ClassificationView | None
    notes: list[str]


class AssetList(BaseModel):
    run: Run | None
    timeframe: str
    sort: str
    items: list[AssetIntelligence]
    notes: list[str]


class AssetDetail(BaseModel):
    asset_id: uuid.UUID
    symbol: str
    run: Run | None
    timeframes: list[AssetIntelligence]
    history: list[ClassificationView]
    benchmark_relation: dict[str, Any] | None


class Cell(BaseModel):
    a: str
    b: str
    status: str
    observations: int
    correlation: str | None
    ci_low: str | None
    ci_high: str | None
    significant: bool | None
    prior_correlation: str | None
    change: str | None


class BenchmarkRelationView(BaseModel):
    symbol: str
    status: str
    observations: int
    correlation: str | None
    beta: str | None
    relative_log_return: str | None


class Correlations(BaseModel):
    run: Run | None
    as_of: datetime | None
    window: int | None
    window_start: datetime | None
    window_end: datetime | None
    symbols: list[str]
    cells: list[Cell]
    benchmark: list[BenchmarkRelationView]
    summary: dict[str, Any]
    notes: list[str]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RunKind = RunKind.LIVE
    sessions: int | None = Field(
        default=None, ge=1, le=750, description="RECONSTRUCTION: past sessions to recompute"
    )


class ConfigurationView(BaseModel):
    code: str
    version: int
    config_id: uuid.UUID
    description: str
    config: dict[str, Any]
    settings: dict[str, Any]
    notes: list[str]


# ------------------------------------------------------------------ helpers
def _view(row: Any) -> ClassificationView:
    return ClassificationView(
        dimension=row.dimension.value,
        timeframe=row.timeframe,
        label=row.label,
        confidence=str(row.confidence),
        probabilities=row.probabilities,
        quality=row.quality.value,
        algorithm=row.algorithm,
        as_of=row.as_of,
        basis=row.basis,
        available_at=row.available_at,
        inputs_available_at=row.inputs_available_at,
        evidence=row.evidence,
        reasons=row.reasons,
    )


def _asset(snapshot: Any, symbol: str, classes: dict[str, Any]) -> AssetIntelligence:
    return AssetIntelligence(
        asset_id=snapshot.asset_id,
        symbol=symbol,
        timeframe=snapshot.timeframe,
        quality=snapshot.quality.value,
        as_of=snapshot.as_of,
        last_event_at=snapshot.last_event_at,
        bars_used=snapshot.bars_used,
        expected_bars=snapshot.expected_bars,
        features=snapshot.feature_values,
        trend=_view(classes["TREND"]) if "TREND" in classes else None,
        volatility=_view(classes["VOLATILITY"]) if "VOLATILITY" in classes else None,
        notes=snapshot.notes,
    )


def _current_run(ctx: IntelligenceContext, session: Any, at: datetime) -> Any:
    return queries.latest_live_run(session, ctx.provider.provider_id, ctx.provider.feed, at)


def _sort_value(item: AssetIntelligence, key: str) -> tuple[int, float, str]:
    raw = item.features.get(key) if key != "symbol" else None
    if key == "symbol":
        return (0, 0.0, item.symbol)
    if raw is None:
        return (1, 0.0, item.symbol)  # missing values last, never as zero
    return (0, -float(raw), item.symbol)


# ------------------------------------------------------------------ endpoints
@router.get("/market", response_model=MarketConditions)
def market(ctx: Context, at: QueryTime | None = None) -> MarketConditions:
    """Broad-market conditions as recorded by the newest finished live run by ``at``:
    benchmark trend and volatility per timeframe, market stability, universe participation."""
    when = at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        run = _current_run(ctx, session, when)
        if run is None:
            return MarketConditions(
                available=False,
                run=None,
                as_of=None,
                benchmark_symbol=ctx.settings.benchmark_symbol,
                classifications=[],
                participation=None,
                notes=["No market intelligence run had completed by this time.", DISCLAIMER],
            )
        rows = queries.market_rows(session, run.id)
        summary = queries.summary(session, run.id)
        names = queries.symbols(
            session,
            {summary.benchmark_asset_id} if summary and summary.benchmark_asset_id else set(),
        )
        return MarketConditions(
            available=True,
            run=Run.model_validate(run),
            as_of=run.as_of_end,
            benchmark_symbol=None
            if summary is None or summary.benchmark_asset_id is None
            else names.get(summary.benchmark_asset_id),
            classifications=[_view(r) for r in rows],
            participation=None
            if summary is None
            else Participation(
                status=summary.participation_status,
                members_total=summary.members_total,
                members_evaluated=summary.members_evaluated,
                metrics=summary.participation,
                cross_asset=summary.cross_asset,
                limitations=summary.limitations,
            ),
            notes=[
                "Market trend and volatility are measured on the benchmark; participation "
                "covers the qualified universe only.",
                DISCLAIMER,
            ],
        )


@router.get("/regimes/history", response_model=RegimeHistory)
def regime_history(
    ctx: Context,
    timeframe: Timeframe = "1Day",
    start: QueryTime | None = None,
    end: QueryTime | None = None,
    basis: KnowledgeBasis | None = None,
    at: QueryTime | None = None,
) -> RegimeHistory:
    """Market classifications per decision time (default: the last 180 days)."""
    when = at or ctx.clock.now()
    stop = end or when
    begin = start or stop - timedelta(days=180)
    if begin > stop:
        raise InvalidRequestError("start must not be after end")
    with session_scope(ctx.session_factory) as session:
        rows = queries.regime_history(
            session, timeframe=timeframe, start=begin, end=stop, at=when, basis=basis
        )
    points: dict[datetime, RegimePoint] = {}
    for row in rows:
        point = points.setdefault(row.as_of, RegimePoint(as_of=row.as_of, basis=row.basis))
        setattr(point, row.dimension.value.lower(), _view(row))
    return RegimeHistory(
        timeframe=timeframe,
        points=sorted(points.values(), key=lambda p: p.as_of),
        notes=[
            "RECORDED points use only data Atlas had stored at the decision time; "
            "RECONSTRUCTED points recompute past decision times from data retrieved later.",
            DISCLAIMER,
        ],
    )


@router.get("/assets", response_model=AssetList)
def assets(
    ctx: Context,
    timeframe: Timeframe = "1Day",
    sort: Annotated[
        Literal["relative_strength", "roc", "realized_volatility", "adx", "symbol"],
        Query(description="Relative-strength ranking by default"),
    ] = "relative_strength",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    at: QueryTime | None = None,
) -> AssetList:
    """Trend and volatility measurements per instrument of the newest live run."""
    when = at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        run = _current_run(ctx, session, when)
        if run is None:
            return AssetList(run=None, timeframe=timeframe, sort=sort, items=[], notes=[DISCLAIMER])
        items = [_asset(*row) for row in queries.asset_rows(session, run.id, timeframe)]
        items.sort(key=lambda item: _sort_value(item, sort))
        return AssetList(
            run=Run.model_validate(run),
            timeframe=timeframe,
            sort=sort,
            items=items[:limit],
            notes=[
                "Relative strength is against the benchmark over the same bars; instruments "
                "without enough history have no value and are listed last.",
                DISCLAIMER,
            ],
        )


@router.get("/assets/{asset_id}", response_model=AssetDetail)
def asset_detail(asset_id: uuid.UUID, ctx: Context, at: QueryTime | None = None) -> AssetDetail:
    when = at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        names = queries.symbols(session, {asset_id})
        if asset_id not in names:
            raise NotFoundError(f"asset {asset_id} does not exist")
        run = _current_run(ctx, session, when)
        frames: list[AssetIntelligence] = []
        relation = None
        if run is not None:
            for timeframe in TIMEFRAMES:
                for row in queries.asset_rows(session, run.id, timeframe):
                    if row[0].asset_id == asset_id:
                        frames.append(_asset(*row))
            for rel in queries.relationships(session, run.id):
                if rel.kind.value == "BENCHMARK" and rel.asset_a == asset_id:
                    relation = {
                        "status": rel.status,
                        "observations": rel.observations,
                        "correlation": queries.decimal_or_none(rel.correlation),
                        "beta": queries.decimal_or_none(rel.beta),
                        "relative_log_return": queries.decimal_or_none(rel.relative_log_return),
                        "window": rel.window,
                    }
        history = queries.asset_history(session, asset_id, timeframe="1Day", at=when, limit=60)
        return AssetDetail(
            asset_id=asset_id,
            symbol=names[asset_id],
            run=None if run is None else Run.model_validate(run),
            timeframes=frames,
            history=[_view(r) for r in history],
            benchmark_relation=relation,
        )


@router.get("/correlations", response_model=Correlations)
def correlations(
    ctx: Context, run_id: uuid.UUID | None = None, at: QueryTime | None = None
) -> Correlations:
    """Pairwise correlations of daily returns among the qualified universe (newest live
    run, or ``run_id``), with observation counts and confidence intervals."""
    when = at or ctx.clock.now()
    with session_scope(ctx.session_factory) as session:
        run = (
            session.get(IntelligenceRunRecord, run_id)
            if run_id is not None
            else _current_run(ctx, session, when)
        )
        if run is None:
            if run_id is not None:
                raise NotFoundError(f"run {run_id} does not exist")
            return Correlations(
                run=None,
                as_of=None,
                window=None,
                window_start=None,
                window_end=None,
                symbols=[],
                cells=[],
                benchmark=[],
                summary={},
                notes=[DISCLAIMER],
            )
        rows = queries.relationships(session, run.id)
        ids = {r.asset_a for r in rows} | {r.asset_b for r in rows}
        names = queries.symbols(session, ids)
        summary = queries.summary(session, run.id)
        pairs = [r for r in rows if r.kind.value == "PAIR"]
        cells = [
            Cell(
                a=names[r.asset_a],
                b=names[r.asset_b],
                status=r.status,
                observations=r.observations,
                correlation=queries.decimal_or_none(r.correlation),
                ci_low=queries.decimal_or_none(r.ci_low),
                ci_high=queries.decimal_or_none(r.ci_high),
                significant=r.significant,
                prior_correlation=queries.decimal_or_none(r.prior_correlation),
                change=queries.decimal_or_none(r.change),
            )
            for r in pairs
        ]
        bench = [
            BenchmarkRelationView(
                symbol=names[r.asset_a],
                status=r.status,
                observations=r.observations,
                correlation=queries.decimal_or_none(r.correlation),
                beta=queries.decimal_or_none(r.beta),
                relative_log_return=queries.decimal_or_none(r.relative_log_return),
            )
            for r in rows
            if r.kind.value == "BENCHMARK"
        ]
        first = pairs[0] if pairs else (rows[0] if rows else None)
        return Correlations(
            run=Run.model_validate(run),
            as_of=run.as_of_end,
            window=None if first is None else first.window,
            window_start=None if first is None else first.window_start,
            window_end=None if first is None else first.window_end,
            symbols=sorted({c.a for c in cells} | {c.b for c in cells}),
            cells=cells,
            benchmark=sorted(bench, key=lambda b: b.symbol),
            summary={} if summary is None else summary.cross_asset,
            notes=[
                "Correlation of daily log returns describes co-movement in the window only: "
                "it implies no causation and need not persist.",
                "Pairs with too few shared observations have no coefficient (never zero).",
                DISCLAIMER,
            ],
        )


@router.get("/runs", response_model=list[Run])
def runs(
    ctx: Context,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Run]:
    with session_scope(ctx.session_factory) as session:
        rows = session.scalars(
            select(IntelligenceRunRecord)
            .order_by(IntelligenceRunRecord.created_at.desc(), IntelligenceRunRecord.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [Run.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=Run)
def run_detail(run_id: uuid.UUID, ctx: Context) -> Run:
    with session_scope(ctx.session_factory) as session:
        run = session.get(IntelligenceRunRecord, run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} does not exist")
        return Run.model_validate(run)


@router.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Run,
    dependencies=[Depends(require_operator)],
)
def start_run(body: RunRequest, ctx: Context, background: BackgroundTasks) -> Run:
    """Start a LIVE run (now) or a RECONSTRUCTION of past sessions; 409 while one runs."""
    if body.kind is RunKind.LIVE and body.sessions is not None:
        raise InvalidRequestError("sessions applies to RECONSTRUCTION runs only")
    params = {} if body.sessions is None else {"sessions": body.sessions}
    run_id = ctx.service.create_run(body.kind, requested_by="api", params=params)
    background.add_task(execute_blocking, ctx.service, run_id)
    return Run.model_validate(ctx.service.get_run(run_id))


@router.get("/config", response_model=ConfigurationView)
def configuration(ctx: Context) -> ConfigurationView:
    with session_scope(ctx.session_factory) as session:
        record, _ = resolve_config(session, ctx.settings.config)
        return ConfigurationView(
            code=record.code,
            version=record.version,
            config_id=record.id,
            description=record.description,
            config=record.config,
            settings=ctx.settings.model_dump(mode="json"),
            notes=[
                "Defaults are interpretable starting points, not optimized or validated values "
                "(decision D-067).",
            ],
        )


__all__ = ["router"]

"""Point-in-time input gathering for a decision run (Phase 5B).

Every query is bounded by the decision time ``decision_at`` and the knowledge time
``knowledge_at``:

* **RECORDED** (``knowledge_at == decision_at``, the only basis of LIVE runs): a row is
  read only if Atlas had stored it by the decision time: proposals by ``created_at``,
  supersessions by ``recorded_at``, regimes, features and correlations by
  ``available_at`` (and RECORDED basis), liquidity by ``created_at``, LLM analyses by
  ``available_at``, ML predictions by ``predicted_at``, model status by its events'
  ``created_at``, track records by the dataset's ``created_at`` and ``data_known_at``.
* **RECONSTRUCTED** (historical research): rows stored later are admitted when they
  describe what was true by the decision time (a proposal's signal, a regime's ``as_of``,
  an article's publication). Models trained after the decision time are never used.

Nothing here writes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Float, and_, cast, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.ai_engine.coordinator.bundle import (
    AssetFeaturesInput,
    AssetRegimeInput,
    CorrelationInput,
    InputBundle,
    LLMAnalysisInput,
    MLPredictionInput,
    NewsEventsInput,
    ProposalInput,
    RecordedConflictInput,
    RegimeInput,
    TrackRecordInput,
)
from app.ai_engine.coordinator.policy import DecisionPolicy
from app.ai_engine.enums import DecisionMode, LLMTask
from app.asset_discovery.enums import MetricStatus
from app.database.models import (
    Asset,
    CrossAssetRelationshipRecord,
    EventAssetRelationshipRecord,
    EventClassificationRecord,
    LLMAnalysisRecord,
    MarketFeatureSnapshotRecord,
    MLDatasetObservationRecord,
    MLDatasetVersionRecord,
    MLModelStatusEventRecord,
    MLModelVersionRecord,
    MLPredictionRecord,
    RegimeClassificationRecord,
    StrategyConfigurationRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalConflictRecord,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
    StrategyRecord,
    StrategyVersionRecord,
)
from app.database.models.universe import AssetMetricValueRecord
from app.feature_engine.features import FeatureQuality
from app.market_intelligence.enums import KnowledgeBasis, RelationKind, Scope
from app.ml_engine.enums import LabelStatus, ObservationSource, PredictionValidity
from app.news.enums import EventCategory
from app.strategies.enums import Direction, ProposalEventKind

LIQUIDITY_METRIC = "median_dollar_volume"
SENTIMENT_VALUE = {"POSITIVE": 1, "NEGATIVE": -1, "NEUTRAL": 0, "MIXED": 0}


def _recorded(basis: KnowledgeBasis) -> bool:
    return basis is KnowledgeBasis.RECORDED


def proposals(
    session: Session, decision_at: datetime, knowledge_at: datetime, basis: KnowledgeBasis
) -> list[ProposalInput]:
    p = StrategyProposalRecord
    e = StrategyProposalEventRecord
    superseded = (
        select(e.proposal_id)
        .where(
            e.proposal_id == p.id,
            e.kind == ProposalEventKind.SUPERSEDED,
            e.occurred_at <= decision_at,
            e.recorded_at <= knowledge_at,
        )
        .exists()
    )
    stmt = (
        select(
            p,
            Asset.symbol,
            StrategyRecord.family,
            StrategyVersionRecord.version,
            StrategyVersionRecord.logic_version,
            StrategyConfigurationRecord.parameters_sha256,
        )
        .join(Asset, Asset.id == p.asset_id)
        .join(StrategyRecord, StrategyRecord.id == p.strategy_id)
        .join(StrategyVersionRecord, StrategyVersionRecord.id == p.strategy_version_id)
        .join(
            StrategyConfigurationRecord,
            StrategyConfigurationRecord.id == StrategyVersionRecord.configuration_id,
        )
        .where(
            p.signal_at <= decision_at,
            p.data_cutoff <= decision_at,
            p.expires_at > decision_at,
            p.created_at <= knowledge_at,
            ~superseded,
        )
        .order_by(p.id)
    )
    if _recorded(basis):
        stmt = stmt.where(p.basis == KnowledgeBasis.RECORDED)
    rows = session.execute(stmt).all()
    snapshot_ids = {sid for row in rows for sid in row[0].feature_snapshot_ids}
    snapshots = (
        {
            s.id: s
            for s in session.scalars(
                select(StrategyFeatureSnapshotRecord).where(
                    StrategyFeatureSnapshotRecord.id.in_(list(snapshot_ids))
                )
            )
        }
        if snapshot_ids
        else {}
    )
    out: list[ProposalInput] = []
    for rec, symbol, family, version, logic, config_sha in rows:
        snaps = [snapshots[s] for s in rec.feature_snapshot_ids if s in snapshots]
        available = [s.inputs_available_at for s in snaps if s.inputs_available_at is not None]
        out.append(
            ProposalInput(
                id=rec.id,
                strategy_id=rec.strategy_id,
                family=family.value,
                strategy_version_id=rec.strategy_version_id,
                strategy_version=version,
                logic_version=logic,
                configuration_sha256=config_sha,
                asset_id=rec.asset_id,
                symbol=symbol,
                market=rec.market,
                direction=rec.direction,
                timeframe=rec.timeframe,
                mode=rec.mode.value,
                basis=rec.basis,
                signal_at=rec.signal_at,
                data_cutoff=rec.data_cutoff,
                available_at=rec.created_at,
                expires_at=rec.expires_at,
                reference_entry=rec.reference_entry,
                invalidation_level=rec.invalidation_level,
                target_reference=rec.target_reference,
                holding_horizon_periods=rec.holding_horizon_periods,
                signal_strength=rec.signal_strength,
                strength_kind=rec.strength_kind.value,
                feature_snapshot_ids=list(rec.feature_snapshot_ids),
                snapshot_feature_count=sum(s.feature_count for s in snaps),
                snapshot_valid_count=sum(s.valid_count for s in snaps),
                snapshot_basis=KnowledgeBasis.RECORDED
                if snaps and all(s.basis is KnowledgeBasis.RECORDED for s in snaps)
                else KnowledgeBasis.RECONSTRUCTED,
                inputs_available_at=max(available) if available else None,
                evidence_count=len(rec.evidence),
            )
        )
    return out


def recorded_conflicts(
    session: Session, ids: list[uuid.UUID], knowledge_at: datetime
) -> list[RecordedConflictInput]:
    if not ids:
        return []
    c = StrategyProposalConflictRecord
    rows = session.scalars(
        select(c)
        .where(c.proposal_a_id.in_(ids), c.proposal_b_id.in_(ids), c.detected_at <= knowledge_at)
        .order_by(c.id)
    )
    return [
        RecordedConflictInput(
            id=r.id,
            asset_id=r.asset_id,
            proposal_a_id=r.proposal_a_id,
            proposal_b_id=r.proposal_b_id,
            detected_at=r.detected_at,
        )
        for r in rows
    ]


def _regime_rows(
    session: Session,
    *,
    scope: Scope,
    asset_ids: list[uuid.UUID],
    decision_at: datetime,
    knowledge_at: datetime,
    basis: KnowledgeBasis,
    oldest: datetime,
) -> list[RegimeClassificationRecord]:
    r = RegimeClassificationRecord
    stmt = select(r).where(
        r.scope == scope,
        r.as_of <= decision_at,
        r.as_of >= oldest,
        r.available_at <= knowledge_at,
    )
    if scope is Scope.ASSET:
        if not asset_ids:
            return []
        stmt = stmt.where(r.asset_id.in_(asset_ids))
    if _recorded(basis):
        stmt = stmt.where(r.basis == KnowledgeBasis.RECORDED)
    stmt = stmt.ext(distinct_on(r.asset_id, r.timeframe, r.dimension)).order_by(
        r.asset_id, r.timeframe, r.dimension, r.as_of.desc(), r.available_at.desc(), r.id.desc()
    )
    return list(session.scalars(stmt))


def _regime_payload(r: RegimeClassificationRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "scope": r.scope.value,
        "asset_id": r.asset_id,
        "timeframe": r.timeframe,
        "dimension": r.dimension.value,
        "label": r.label,
        "confidence": float(r.confidence),
        "quality": r.quality.value,
        "basis": r.basis,
        "as_of": r.as_of,
        "available_at": r.available_at,
        "algorithm": r.algorithm,
    }


def asset_features(
    session: Session,
    asset_ids: list[uuid.UUID],
    timeframes: set[str],
    decision_at: datetime,
    knowledge_at: datetime,
    basis: KnowledgeBasis,
    oldest: datetime,
) -> list[AssetFeaturesInput]:
    if not asset_ids:
        return []
    f = MarketFeatureSnapshotRecord
    stmt = select(f).where(
        f.asset_id.in_(asset_ids),
        f.timeframe.in_(sorted(timeframes)),
        f.as_of <= decision_at,
        f.as_of >= oldest,
        f.available_at <= knowledge_at,
        f.quality != FeatureQuality.INSUFFICIENT_DATA,
    )
    if _recorded(basis):
        stmt = stmt.where(f.basis == KnowledgeBasis.RECORDED)
    snaps = {
        (s.asset_id, s.timeframe): s
        for s in session.scalars(
            stmt.ext(distinct_on(f.asset_id, f.timeframe)).order_by(
                f.asset_id, f.timeframe, f.as_of.desc(), f.available_at.desc(), f.id.desc()
            )
        )
    }
    m = AssetMetricValueRecord
    liquidity = {
        row.asset_id: row
        for row in session.scalars(
            select(m)
            .where(
                m.asset_id.in_(asset_ids),
                m.metric == LIQUIDITY_METRIC,
                m.status == MetricStatus.OK,
                m.as_of <= decision_at,
                m.as_of >= oldest,
                m.created_at <= knowledge_at,
            )
            .ext(distinct_on(m.asset_id))
            .order_by(m.asset_id, m.as_of.desc(), m.id.desc())
        )
    }
    out = []
    for asset_id in sorted(asset_ids, key=str):
        for timeframe in sorted(timeframes):
            snap = snaps.get((asset_id, timeframe))
            liq = liquidity.get(asset_id)
            if snap is None and liq is None:
                continue
            out.append(
                AssetFeaturesInput(
                    asset_id=asset_id,
                    timeframe=timeframe,
                    snapshot_id=None if snap is None else snap.id,
                    feature_set_version=None if snap is None else snap.feature_set_version,
                    features={}
                    if snap is None
                    else {
                        k: None if v is None else float(v)
                        for k, v in sorted(snap.feature_values.items())
                    },
                    quality=None if snap is None else snap.quality.value,
                    basis=None if snap is None else snap.basis,
                    as_of=None if snap is None else snap.as_of,
                    available_at=None if snap is None else snap.available_at,
                    liquidity_metric_id=None if liq is None else liq.id,
                    median_dollar_volume=None
                    if liq is None or liq.value is None
                    else float(liq.value),
                    liquidity_as_of=None if liq is None else liq.as_of,
                )
            )
    return out


def correlations(
    session: Session,
    asset_ids: list[uuid.UUID],
    decision_at: datetime,
    knowledge_at: datetime,
    basis: KnowledgeBasis,
    oldest: datetime,
) -> list[CorrelationInput]:
    if len(asset_ids) < 2:
        return []
    r = CrossAssetRelationshipRecord
    stmt = select(r).where(
        r.kind == RelationKind.PAIR,
        r.status == "OK",
        r.asset_a.in_(asset_ids),
        r.asset_b.in_(asset_ids),
        r.as_of <= decision_at,
        r.as_of >= oldest,
        r.available_at <= knowledge_at,
    )
    if _recorded(basis):
        stmt = stmt.where(r.basis == KnowledgeBasis.RECORDED)
    rows = session.scalars(
        stmt.ext(distinct_on(r.asset_a, r.asset_b)).order_by(
            r.asset_a,
            r.asset_b,
            r.as_of.desc(),
            r.available_at.desc(),
            r.window.desc(),
            r.id.desc(),
        )
    )
    return [
        CorrelationInput(
            id=x.id,
            asset_a=x.asset_a,
            asset_b=x.asset_b,
            correlation=float(x.correlation),
            window=x.window,
            observations=x.observations,
            as_of=x.as_of,
            available_at=x.available_at,
        )
        for x in rows
        if x.correlation is not None
    ]


def news_events(
    session: Session,
    asset_ids: list[uuid.UUID],
    decision_at: datetime,
    knowledge_at: datetime,
    lookback: timedelta,
) -> list[NewsEventsInput]:
    if not asset_ids:
        return []
    rel = EventAssetRelationshipRecord
    cls = EventClassificationRecord
    start = decision_at - lookback
    rows = session.execute(
        select(rel.asset_id, rel.event_id, rel.event_time, rel.available_at, cls.category)
        .join(cls, and_(cls.event_id == rel.event_id, cls.is_primary.is_(True)), isouter=True)
        .where(
            rel.asset_id.in_(asset_ids),
            rel.event_time > start,
            rel.event_time <= decision_at,
            rel.available_at <= knowledge_at,
        )
        .order_by(rel.asset_id, rel.event_id)
    ).all()
    grouped: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row.asset_id].append(row)
    out = []
    for asset_id in sorted(grouped, key=str):
        items = grouped[asset_id]
        ids = sorted({r.event_id for r in items})
        categories: dict[str, int] = defaultdict(int)
        for r in {r.event_id: r for r in items}.values():
            category = r.category.value if isinstance(r.category, EventCategory) else "UNKNOWN"
            categories[category] += 1
        out.append(
            NewsEventsInput(
                asset_id=asset_id,
                window_start=start,
                window_end=decision_at,
                event_count=len(ids),
                event_ids=ids,
                categories=dict(sorted(categories.items())),
                latest_event_at=max(r.event_time for r in items),
                available_at=max(r.available_at for r in items),
            )
        )
    return out


def llm_analyses(
    session: Session,
    asset_ids: list[uuid.UUID],
    decision_at: datetime,
    knowledge_at: datetime,
    lookback: timedelta,
) -> list[LLMAnalysisInput]:
    """Usable NEWS_ANALYSIS records about the assets, stored by the knowledge time, about
    articles published in the look-back window before the decision."""
    if not asset_ids:
        return []
    a = LLMAnalysisRecord
    rows = session.scalars(
        select(a)
        .where(
            a.task == LLMTask.NEWS_ANALYSIS,
            a.usable.is_(True),
            a.available_at <= knowledge_at,
            a.subject_time <= decision_at,
            a.subject_time > decision_at - lookback,
            a.asset_ids.overlap(asset_ids),
        )
        .order_by(a.available_at, a.id)
    )
    wanted = {str(x) for x in asset_ids}
    out = []
    for r in rows:
        sentiment: dict[str, int] = {}
        for entry in r.analysis.get("assets", []):
            asset = str(entry.get("asset_id"))
            if asset in wanted and asset in {str(x) for x in r.asset_ids}:
                sentiment[asset] = SENTIMENT_VALUE.get(str(entry.get("sentiment")), 0)
        if not sentiment:
            continue
        out.append(
            LLMAnalysisInput(
                id=r.id,
                task=r.task.value,
                subject_ref=r.subject_ref,
                article_id=r.article_id,
                asset_ids=sorted(r.asset_ids, key=str),
                sentiment=dict(sorted(sentiment.items())),
                event_type=r.analysis.get("event_type"),
                catalysts=[str(x) for x in r.analysis.get("catalysts", [])][:10],
                summary=r.analysis.get("summary"),
                provider=r.provider,
                model=r.model,
                prompt_version=r.prompt_version,
                subject_time=r.subject_time,
                available_at=r.available_at,
            )
        )
    return out


def _status_at(
    session: Session, version_ids: list[uuid.UUID], at: datetime
) -> dict[uuid.UUID, str]:
    if not version_ids:
        return {}
    e = MLModelStatusEventRecord
    rows = session.execute(
        select(e.model_version_id, e.to_status)
        .where(e.model_version_id.in_(version_ids), e.created_at <= at)
        .ext(distinct_on(e.model_version_id))
        .order_by(e.model_version_id, e.id.desc())
    ).all()
    return {row.model_version_id: row.to_status.value for row in rows}


def ml_predictions(
    session: Session,
    asset_ids: list[uuid.UUID],
    decision_at: datetime,
    knowledge_at: datetime,
    basis: KnowledgeBasis,
    oldest: datetime,
) -> list[MLPredictionInput]:
    if not asset_ids:
        return []
    p = MLPredictionRecord
    m = MLModelVersionRecord
    predicted_by = decision_at if _recorded(basis) else knowledge_at
    rows = session.execute(
        select(p, m)
        .join(m, m.id == p.model_version_id)
        .where(
            p.asset_id.in_(asset_ids),
            p.validity == PredictionValidity.VALID,
            p.decision_at <= decision_at,
            p.decision_at >= oldest,
            p.predicted_at <= predicted_by,
            # A model trained after the decision time would leak later data.
            m.created_at <= decision_at,
        )
        .ext(distinct_on(p.model_version_id, p.asset_id, p.direction, p.timeframe))
        .order_by(
            p.model_version_id,
            p.asset_id,
            p.direction,
            p.timeframe,
            p.decision_at.desc(),
            p.predicted_at.desc(),
        )
    ).all()
    statuses = _status_at(session, list({m_.id for _, m_ in rows}), decision_at)
    out = []
    for pred, model in rows:
        summary = model.evaluation_summary or {}
        calibration = dict(summary.get("calibration") or {})
        test = dict(summary.get("test") or {})
        if calibration or test.get("ece") is not None:
            calibration["test_ece"] = test.get("ece")
        target = model.target_definition or {}
        barrier = target.get("barrier") or {}
        out.append(
            MLPredictionInput(
                id=pred.id,
                model_version_id=model.id,
                model_name=model.model_name,
                model_version=model.version,
                model_type=model.model_type.value,
                status_at_decision=statuses.get(model.id, "UNKNOWN"),
                mode=pred.mode.value,
                asset_id=pred.asset_id,
                direction=pred.direction,
                timeframe=pred.timeframe,
                strategy_id=pred.strategy_id,
                decision_at=pred.decision_at,
                predicted_at=pred.predicted_at,
                output_kind=pred.output_kind.value,
                output=pred.output or {},
                feature_snapshot_sha256=pred.feature_snapshot_sha256,
                validation_status=model.validation_status.value,
                target_kind=str(target.get("kind")),
                barrier_source=barrier.get("source") if isinstance(barrier, dict) else None,
                horizon_periods=target.get("horizon_periods"),
                calibration=calibration or None,
                test_metrics=test,
                test_baseline=dict(summary.get("test_baseline") or {}),
                model_created_at=model.created_at,
            )
        )
    return out


def track_records(
    session: Session,
    keys: set[tuple[str, Direction, str]],
    decision_at: datetime,
    knowledge_at: datetime,
) -> list[TrackRecordInput]:
    """Per (strategy, direction, timeframe): matured outcomes of the newest stored
    strategy dataset known by the decision time."""
    if not keys:
        return []
    v = MLDatasetVersionRecord
    o = MLDatasetObservationRecord
    versions = list(
        session.scalars(
            select(v)
            .where(
                v.source.in_(
                    [ObservationSource.STRATEGY_REPLAY, ObservationSource.STRATEGY_PROPOSALS]
                ),
                v.data_known_at <= decision_at,
                v.created_at <= knowledge_at,
            )
            .order_by(v.data_known_at.desc(), v.created_at.desc())
        )
    )
    out: list[TrackRecordInput] = []
    strategies = sorted({k[0] for k in keys})
    done: set[tuple[str, Direction, str]] = set()
    net = cast(o.outcome["net_return"].astext, Float)
    for version in versions:
        rows = session.execute(
            select(
                o.strategy_id,
                o.direction,
                o.timeframe,
                func.count(),
                func.avg(net),
                func.avg(cast(net > 0, Float)),
                func.max(o.label_end_at),
            )
            .where(
                o.dataset_version_id == version.id,
                o.strategy_id.in_(strategies),
                o.label_status == LabelStatus.MATURED,
                o.label_end_at <= decision_at,
            )
            .group_by(o.strategy_id, o.direction, o.timeframe)
        ).all()
        for strategy_id, direction, timeframe, n, mean, hit, last in rows:
            key = (strategy_id, direction, timeframe)
            if key not in keys or key in done:
                continue
            done.add(key)
            out.append(
                TrackRecordInput(
                    strategy_id=strategy_id,
                    direction=direction,
                    timeframe=timeframe,
                    dataset_version_id=version.id,
                    dataset_source=version.source.value,
                    data_known_at=version.data_known_at,
                    observations=int(n),
                    mean_net_return=None if mean is None else float(mean),
                    hit_rate=None if hit is None else float(hit),
                    cost_assumption_bps=(version.target_definition or {}).get(
                        "round_trip_cost_bps"
                    ),
                    last_label_end_at=last,
                )
            )
    return sorted(out, key=lambda t: t.ref)


def gather(
    session: Session,
    *,
    decision_at: datetime,
    knowledge_at: datetime,
    basis: KnowledgeBasis,
    mode: DecisionMode,
    policy: DecisionPolicy,
) -> InputBundle:
    if knowledge_at < decision_at:
        raise ValueError("knowledge_at precedes decision_at")
    if _recorded(basis) and knowledge_at != decision_at:
        raise ValueError("a RECORDED run knows exactly what was stored by its decision time")
    oldest = decision_at - timedelta(hours=policy.max_context_age_hours)
    found = proposals(session, decision_at, knowledge_at, basis)
    asset_ids = sorted({p.asset_id for p in found}, key=str)
    timeframes = {p.timeframe for p in found} | {"1Day"}
    notes: list[str] = []
    market = [
        RegimeInput.model_validate(_regime_payload(r))
        for r in _regime_rows(
            session,
            scope=Scope.MARKET,
            asset_ids=[],
            decision_at=decision_at,
            knowledge_at=knowledge_at,
            basis=basis,
            oldest=oldest,
        )
    ]
    if not market:
        notes.append("no market regime classification was available at the decision time")
    assets = [
        AssetRegimeInput.model_validate(_regime_payload(r))
        for r in _regime_rows(
            session,
            scope=Scope.ASSET,
            asset_ids=asset_ids,
            decision_at=decision_at,
            knowledge_at=knowledge_at,
            basis=basis,
            oldest=oldest,
        )
    ]
    lookback = timedelta(hours=policy.news_lookback_hours)
    ml = ml_predictions(session, asset_ids, decision_at, knowledge_at, basis, oldest)
    if not ml and found:
        notes.append("no ML prediction for these assets was recorded by the decision time")
    llm = llm_analyses(session, asset_ids, decision_at, knowledge_at, lookback)
    if not llm and found:
        notes.append("no usable LLM news analysis for these assets at the decision time")
    keys = {(p.strategy_id, p.direction, p.timeframe) for p in found}
    return InputBundle(
        decision_at=decision_at,
        knowledge_at=knowledge_at,
        basis=basis,
        mode=mode,
        proposals=found,
        recorded_conflicts=recorded_conflicts(session, [p.id for p in found], knowledge_at),
        market_regimes=sorted(market, key=lambda r: r.id),
        asset_regimes=sorted(assets, key=lambda r: r.id),
        asset_features=asset_features(
            session, asset_ids, timeframes, decision_at, knowledge_at, basis, oldest
        ),
        correlations=sorted(
            correlations(session, asset_ids, decision_at, knowledge_at, basis, oldest),
            key=lambda c: c.id,
        ),
        news=news_events(session, asset_ids, decision_at, knowledge_at, lookback),
        llm_analyses=sorted(llm, key=lambda a: str(a.id)),
        ml_predictions=sorted(ml, key=lambda m: str(m.id)),
        track_records=track_records(session, keys, decision_at, knowledge_at),
        notes=notes,
    )


def models_in_status(session: Session, at: datetime, status: str) -> list[MLModelVersionRecord]:
    """Model versions whose newest status event stored by ``at`` is ``status``."""
    e = MLModelStatusEventRecord
    newest = (
        select(e.model_version_id, e.to_status)
        .where(e.created_at <= at)
        .ext(distinct_on(e.model_version_id))
        .order_by(e.model_version_id, e.id.desc())
        .subquery()
    )
    return list(
        session.scalars(
            select(MLModelVersionRecord)
            .join(newest, newest.c.model_version_id == MLModelVersionRecord.id)
            .where(newest.c.to_status == status)
            .order_by(MLModelVersionRecord.model_name, MLModelVersionRecord.version)
        )
    )


__all__ = [
    "LIQUIDITY_METRIC",
    "SENTIMENT_VALUE",
    "gather",
    "models_in_status",
]

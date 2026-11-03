"""Read models of the Strategy Laboratory (Phase 4A) for the API and dashboard.

Proposal status is derived at read time: SUPERSEDED if a newer proposal superseded it,
EXPIRED once its expiry passed, ACTIVE otherwise. Nothing here computes performance:
no proposal has been traded, so there is none to report.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, case, exists, func, select
from sqlalchemy.orm import Session

from app.database.models import (
    Asset,
    StrategyConfigurationRecord,
    StrategyEvaluationErrorRecord,
    StrategyEvaluationRecord,
    StrategyEvaluationSnapshotRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
    StrategyStatusEventRecord,
    StrategyVersionRecord,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import (
    Direction,
    ErrorStage,
    EvaluationMode,
    EvaluationStatus,
    ProposalEventKind,
    ProposalStatus,
    StrengthKind,
    TriggerKind,
)

DISCLAIMER = (
    "Proposals are hypotheses produced by strategy code from point-in-time features. They "
    "are not approved trades, carry no position size and were not sent anywhere; signal "
    "strength is an uncalibrated score, not a probability. No strategy has been traded or "
    "backtested, so there are no performance statistics."
)


class VersionView(BaseModel):
    id: uuid.UUID
    strategy_id: str
    version: int
    logic_version: str
    configuration_id: uuid.UUID
    parameters: dict[str, Any]
    parameters_sha256: str
    name: str
    description: str
    implemented: bool
    supported_asset_classes: list[str]
    supported_timeframes: list[str]
    schedule: dict[str, Any]
    required_features: dict[str, Any]
    feature_registry_version: str
    reason: str
    created_by: str
    created_at: datetime


class ConfigurationView(BaseModel):
    id: uuid.UUID
    strategy_id: str
    parameters: dict[str, Any]
    parameters_sha256: str
    schema_sha256: str
    created_by: str
    created_at: datetime
    versions: list[int]


class StatusEventView(BaseModel):
    enabled: bool
    reason: str
    actor: str
    created_at: datetime


class ErrorView(BaseModel):
    stage: ErrorStage
    error_type: str
    message: str
    occurred_at: datetime


class EvaluationView(BaseModel):
    id: uuid.UUID
    strategy_id: str
    strategy_version_id: uuid.UUID
    version: int
    asset_id: uuid.UUID
    symbol: str
    timeframe: str
    mode: EvaluationMode
    basis: KnowledgeBasis
    trigger: TriggerKind
    trigger_ref: str
    attempt: int
    status: EvaluationStatus
    data_cutoff: datetime
    known_at: datetime
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    proposals_count: int
    skip_reasons: list[dict[str, Any]]
    notes: list[str]
    worker_id: str


class SnapshotView(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    timeframe: str
    data_cutoff: datetime
    known_at: datetime
    basis: KnowledgeBasis
    provider: str
    feed: str
    adjustment: str
    feature_registry_version: str
    feature_count: int
    valid_count: int
    inputs_available_at: datetime | None
    last_period_event_at: datetime | None
    content_sha256: str
    created_at: datetime
    features: dict[str, Any]


class ProposalEventView(BaseModel):
    kind: ProposalEventKind
    occurred_at: datetime
    recorded_at: datetime
    superseded_by: uuid.UUID | None


class ProposalView(BaseModel):
    id: uuid.UUID
    evaluation_id: uuid.UUID
    strategy_id: str
    strategy_version_id: uuid.UUID
    version: int
    asset_id: uuid.UUID
    symbol: str
    market: str
    direction: Direction
    timeframe: str
    mode: EvaluationMode
    basis: KnowledgeBasis
    signal_at: datetime
    data_cutoff: datetime
    feature_snapshot_ids: list[uuid.UUID]
    reference_entry: Decimal
    invalidation_level: Decimal | None
    target_reference: Decimal | None
    holding_horizon_periods: int
    signal_strength: Decimal
    strength_kind: StrengthKind
    evidence: list[dict[str, Any]]
    reasoning: str
    expires_at: datetime
    created_at: datetime
    status: ProposalStatus


def version_view(
    version: StrategyVersionRecord, config: StrategyConfigurationRecord
) -> VersionView:
    return VersionView(
        id=version.id,
        strategy_id=version.strategy_id,
        version=version.version,
        logic_version=version.logic_version,
        configuration_id=version.configuration_id,
        parameters=config.parameters,
        parameters_sha256=config.parameters_sha256,
        name=version.name,
        description=version.description,
        implemented=version.implemented,
        supported_asset_classes=list(version.supported_asset_classes),
        supported_timeframes=list(version.supported_timeframes),
        schedule=version.schedule,
        required_features=version.required_features,
        feature_registry_version=version.feature_registry_version,
        reason=version.reason,
        created_by=version.created_by,
        created_at=version.created_at,
    )


def versions(session: Session, strategy_id: str) -> list[VersionView]:
    rows = session.execute(
        select(StrategyVersionRecord, StrategyConfigurationRecord)
        .join(
            StrategyConfigurationRecord,
            StrategyConfigurationRecord.id == StrategyVersionRecord.configuration_id,
        )
        .where(StrategyVersionRecord.strategy_id == strategy_id)
        .order_by(StrategyVersionRecord.version.desc())
    ).all()
    return [version_view(v, c) for v, c in rows]


def configurations(session: Session, strategy_id: str) -> list[ConfigurationView]:
    used: dict[uuid.UUID, list[int]] = {}
    for config_id, number in session.execute(
        select(StrategyVersionRecord.configuration_id, StrategyVersionRecord.version).where(
            StrategyVersionRecord.strategy_id == strategy_id
        )
    ):
        used.setdefault(config_id, []).append(number)
    rows = session.scalars(
        select(StrategyConfigurationRecord)
        .where(StrategyConfigurationRecord.strategy_id == strategy_id)
        .order_by(StrategyConfigurationRecord.created_at.desc())
    ).all()
    return [
        ConfigurationView(
            id=c.id,
            strategy_id=c.strategy_id,
            parameters=c.parameters,
            parameters_sha256=c.parameters_sha256,
            schema_sha256=c.schema_sha256,
            created_by=c.created_by,
            created_at=c.created_at,
            versions=sorted(used.get(c.id, [])),
        )
        for c in rows
    ]


def status_history(session: Session, strategy_id: str) -> list[StatusEventView]:
    rows = session.scalars(
        select(StrategyStatusEventRecord)
        .where(StrategyStatusEventRecord.strategy_id == strategy_id)
        .order_by(StrategyStatusEventRecord.id.desc())
    ).all()
    return [
        StatusEventView(enabled=r.enabled, reason=r.reason, actor=r.actor, created_at=r.created_at)
        for r in rows
    ]


def counts(session: Session, now: datetime) -> dict[str, dict[str, Any]]:
    """Per strategy: evaluations, last evaluation time, proposals total and active."""
    e, p = StrategyEvaluationRecord, StrategyProposalRecord
    out: dict[str, dict[str, Any]] = {}
    for sid, total, last in session.execute(
        select(e.strategy_id, func.count(), func.max(e.started_at)).group_by(e.strategy_id)
    ):
        out.setdefault(sid, {})["evaluations"] = int(total)
        out[sid]["last_evaluation_at"] = last
    superseded = exists().where(
        StrategyProposalEventRecord.proposal_id == p.id,
        StrategyProposalEventRecord.kind == ProposalEventKind.SUPERSEDED,
    )
    active = case((~superseded & (p.expires_at > now), 1), else_=0)
    for sid, total, live in session.execute(
        select(p.strategy_id, func.count(), func.sum(active)).group_by(p.strategy_id)
    ):
        out.setdefault(sid, {})["proposals"] = int(total)
        out[sid]["active_proposals"] = int(live or 0)
    return out


def _evaluation_select() -> Select[StrategyEvaluationRecord, int, str]:
    e = StrategyEvaluationRecord
    return (
        select(e, StrategyVersionRecord.version, Asset.symbol)
        .join(StrategyVersionRecord, StrategyVersionRecord.id == e.strategy_version_id)
        .join(Asset, Asset.id == e.asset_id)
    )


def _evaluation_view(e: StrategyEvaluationRecord, version: int, symbol: str) -> EvaluationView:
    duration = (
        None
        if e.finished_at is None
        else int((e.finished_at - e.started_at).total_seconds() * 1000)
    )
    return EvaluationView(
        id=e.id,
        strategy_id=e.strategy_id,
        strategy_version_id=e.strategy_version_id,
        version=version,
        asset_id=e.asset_id,
        symbol=symbol,
        timeframe=e.timeframe,
        mode=e.mode,
        basis=e.basis,
        trigger=e.trigger,
        trigger_ref=e.trigger_ref,
        attempt=e.attempt,
        status=e.status,
        data_cutoff=e.data_cutoff,
        known_at=e.known_at,
        started_at=e.started_at,
        finished_at=e.finished_at,
        duration_ms=duration,
        proposals_count=e.proposals_count,
        skip_reasons=e.skip_reasons,
        notes=e.notes,
        worker_id=e.worker_id,
    )


def evaluations(
    session: Session,
    *,
    strategy_id: str | None = None,
    asset_id: uuid.UUID | None = None,
    status: EvaluationStatus | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[EvaluationView]:
    e = StrategyEvaluationRecord
    stmt = _evaluation_select()
    if strategy_id is not None:
        stmt = stmt.where(e.strategy_id == strategy_id)
    if asset_id is not None:
        stmt = stmt.where(e.asset_id == asset_id)
    if status is not None:
        stmt = stmt.where(e.status == status)
    if before is not None:
        stmt = stmt.where(e.started_at < before)
    rows = session.execute(stmt.order_by(e.started_at.desc(), e.id.desc()).limit(limit)).all()
    return [_evaluation_view(r[0], r[1], r[2]) for r in rows]


def evaluation(session: Session, evaluation_id: uuid.UUID) -> EvaluationView | None:
    row = session.execute(
        _evaluation_select().where(StrategyEvaluationRecord.id == evaluation_id)
    ).first()
    return None if row is None else _evaluation_view(row[0], row[1], row[2])


def evaluation_errors(session: Session, evaluation_id: uuid.UUID) -> list[ErrorView]:
    rows = session.scalars(
        select(StrategyEvaluationErrorRecord)
        .where(StrategyEvaluationErrorRecord.evaluation_id == evaluation_id)
        .order_by(StrategyEvaluationErrorRecord.id)
    ).all()
    return [
        ErrorView(
            stage=r.stage, error_type=r.error_type, message=r.message, occurred_at=r.occurred_at
        )
        for r in rows
    ]


def snapshot_view(s: StrategyFeatureSnapshotRecord) -> SnapshotView:
    return SnapshotView(
        id=s.id,
        asset_id=s.asset_id,
        timeframe=s.timeframe,
        data_cutoff=s.data_cutoff,
        known_at=s.known_at,
        basis=s.basis,
        provider=s.provider,
        feed=s.feed,
        adjustment=s.adjustment.value,
        feature_registry_version=s.feature_registry_version,
        feature_count=s.feature_count,
        valid_count=s.valid_count,
        inputs_available_at=s.inputs_available_at,
        last_period_event_at=s.last_period_event_at,
        content_sha256=s.content_sha256,
        created_at=s.created_at,
        features=s.features,
    )


def evaluation_snapshots(session: Session, evaluation_id: uuid.UUID) -> list[SnapshotView]:
    rows = session.scalars(
        select(StrategyFeatureSnapshotRecord)
        .join(
            StrategyEvaluationSnapshotRecord,
            StrategyEvaluationSnapshotRecord.snapshot_id == StrategyFeatureSnapshotRecord.id,
        )
        .where(StrategyEvaluationSnapshotRecord.evaluation_id == evaluation_id)
    ).all()
    return [snapshot_view(s) for s in rows]


def snapshot(session: Session, snapshot_id: uuid.UUID) -> SnapshotView | None:
    row = session.get(StrategyFeatureSnapshotRecord, snapshot_id)
    return None if row is None else snapshot_view(row)


def _proposal_select() -> Select[StrategyProposalRecord, int, str]:
    p = StrategyProposalRecord
    return (
        select(p, StrategyVersionRecord.version, Asset.symbol)
        .join(StrategyVersionRecord, StrategyVersionRecord.id == p.strategy_version_id)
        .join(Asset, Asset.id == p.asset_id)
    )


def _status(p: StrategyProposalRecord, superseded: bool, now: datetime) -> ProposalStatus:
    if superseded:
        return ProposalStatus.SUPERSEDED
    return ProposalStatus.EXPIRED if p.expires_at <= now else ProposalStatus.ACTIVE


def _proposal_views(session: Session, rows: Sequence[Any], now: datetime) -> list[ProposalView]:
    ids = [r[0].id for r in rows]
    superseded = (
        set(
            session.scalars(
                select(StrategyProposalEventRecord.proposal_id).where(
                    StrategyProposalEventRecord.proposal_id.in_(ids),
                    StrategyProposalEventRecord.kind == ProposalEventKind.SUPERSEDED,
                    StrategyProposalEventRecord.occurred_at <= now,
                )
            )
        )
        if ids
        else set()
    )
    views = []
    for p, version, symbol in rows:
        views.append(
            ProposalView(
                id=p.id,
                evaluation_id=p.evaluation_id,
                strategy_id=p.strategy_id,
                strategy_version_id=p.strategy_version_id,
                version=version,
                asset_id=p.asset_id,
                symbol=symbol,
                market=p.market,
                direction=p.direction,
                timeframe=p.timeframe,
                mode=p.mode,
                basis=p.basis,
                signal_at=p.signal_at,
                data_cutoff=p.data_cutoff,
                feature_snapshot_ids=list(p.feature_snapshot_ids),
                reference_entry=p.reference_entry,
                invalidation_level=p.invalidation_level,
                target_reference=p.target_reference,
                holding_horizon_periods=p.holding_horizon_periods,
                signal_strength=p.signal_strength,
                strength_kind=p.strength_kind,
                evidence=p.evidence,
                reasoning=p.reasoning,
                expires_at=p.expires_at,
                created_at=p.created_at,
                status=_status(p, p.id in superseded, now),
            )
        )
    return views


def proposals(
    session: Session,
    now: datetime,
    *,
    strategy_id: str | None = None,
    asset_id: uuid.UUID | None = None,
    status: ProposalStatus | None = None,
    direction: Direction | None = None,
    evaluation_id: uuid.UUID | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[ProposalView]:
    p = StrategyProposalRecord
    stmt = _proposal_select()
    if strategy_id is not None:
        stmt = stmt.where(p.strategy_id == strategy_id)
    if asset_id is not None:
        stmt = stmt.where(p.asset_id == asset_id)
    if direction is not None:
        stmt = stmt.where(p.direction == direction)
    if evaluation_id is not None:
        stmt = stmt.where(p.evaluation_id == evaluation_id)
    if before is not None:
        stmt = stmt.where(p.signal_at < before)
    superseded = exists().where(
        StrategyProposalEventRecord.proposal_id == p.id,
        StrategyProposalEventRecord.kind == ProposalEventKind.SUPERSEDED,
        StrategyProposalEventRecord.occurred_at <= now,
    )
    if status is ProposalStatus.SUPERSEDED:
        stmt = stmt.where(superseded)
    elif status is ProposalStatus.EXPIRED:
        stmt = stmt.where(~superseded, p.expires_at <= now)
    elif status is ProposalStatus.ACTIVE:
        stmt = stmt.where(~superseded, p.expires_at > now)
    rows = session.execute(stmt.order_by(p.signal_at.desc(), p.id.desc()).limit(limit)).all()
    return _proposal_views(session, rows, now)


def proposal(session: Session, proposal_id: uuid.UUID, now: datetime) -> ProposalView | None:
    row = session.execute(
        _proposal_select().where(StrategyProposalRecord.id == proposal_id)
    ).first()
    return None if row is None else _proposal_views(session, [row], now)[0]


def proposal_events(session: Session, proposal_id: uuid.UUID) -> list[ProposalEventView]:
    rows = session.scalars(
        select(StrategyProposalEventRecord)
        .where(StrategyProposalEventRecord.proposal_id == proposal_id)
        .order_by(StrategyProposalEventRecord.id)
    ).all()
    return [
        ProposalEventView(
            kind=r.kind,
            occurred_at=r.occurred_at,
            recorded_at=r.recorded_at,
            superseded_by=r.superseded_by,
        )
        for r in rows
    ]


__all__ = [
    "DISCLAIMER",
    "ConfigurationView",
    "ErrorView",
    "EvaluationView",
    "ProposalEventView",
    "ProposalView",
    "SnapshotView",
    "StatusEventView",
    "VersionView",
    "configurations",
    "counts",
    "evaluation",
    "evaluation_errors",
    "evaluation_snapshots",
    "evaluations",
    "proposal",
    "proposal_events",
    "proposals",
    "snapshot",
    "status_history",
    "versions",
]

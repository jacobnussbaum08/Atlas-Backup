"""Shared setup for the AI decision engine integration tests (Phase 5B).

Everything here is TEST FIXTURE material written straight into the database: invented
assets, strategy versions, evaluations, feature snapshots and proposals, a market regime
and ML predictions. Every row's storage time (``created_at`` / ``available_at``) is set
explicitly, so point-in-time behaviour does not depend on the wall clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    Asset,
    AssetClass,
    AssetStatus,
    IntelligenceConfigRecord,
    IntelligenceRunRecord,
    RegimeClassificationRecord,
    StrategyConfigurationRecord,
    StrategyEvaluationRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalRecord,
    StrategyRecord,
    StrategyVersionRecord,
)
from app.database.sessions import session_scope
from app.market_intelligence.enums import (
    ClassificationQuality,
    Dimension,
    KnowledgeBasis,
    RunKind,
    RunStatus,
    Scope,
)
from app.strategies.enums import (
    Direction,
    EvaluationMode,
    EvaluationStatus,
    SignalState,
    StrategyFamily,
    StrengthKind,
    TriggerKind,
)

# A Friday session close (20:00 UTC in EDT) plus one hour: the decision time used below.
DECISION_AT = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
NOW = DECISION_AT + timedelta(days=2)  # the coordinator's (fixed) clock


def make_asset(factory: sessionmaker[Session], symbol: str) -> uuid.UUID:
    with session_scope(factory) as session:
        asset = Asset(
            id=uuid.uuid4(),
            symbol=symbol,
            asset_class=AssetClass.EQUITY,
            name=f"{symbol} Fixture Corp",
            exchange_code="XNAS",
            quote_currency="USD",
            status=AssetStatus.ACTIVE,
        )
        session.add(asset)
        return asset.id


def make_strategy(
    factory: sessionmaker[Session], strategy_id: str, family: StrategyFamily
) -> uuid.UUID:
    with session_scope(factory) as session:
        session.add(StrategyRecord(id=strategy_id, family=family))
        session.flush()
        config = StrategyConfigurationRecord(
            id=uuid.uuid4(),
            strategy_id=strategy_id,
            parameters={"fixture": True},
            parameters_sha256=uuid.uuid4().hex * 2,
            schema_sha256="0" * 64,
            created_by="test",
        )
        session.add(config)
        session.flush()
        version = StrategyVersionRecord(
            id=uuid.uuid4(),
            strategy_id=strategy_id,
            version=1,
            logic_version=f"{strategy_id}-fixture",
            configuration_id=config.id,
            name=f"{strategy_id} (TEST FIXTURE)",
            description="fixture",
            implemented=True,
            supported_asset_classes=["EQUITY"],
            supported_timeframes=["1Day"],
            schedule={},
            required_features={},
            feature_registry_version="fixture",
            reason="fixture",
            created_by="test",
        )
        session.add(version)
        return version.id


def make_proposal(
    factory: sessionmaker[Session],
    *,
    strategy_id: str,
    version_id: uuid.UUID,
    asset_id: uuid.UUID,
    direction: Direction = Direction.LONG,
    stored_at: datetime,
    cutoff: datetime | None = None,
    strength: str = "0.80",
    entry: str = "100",
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """A proposal (with its evaluation and snapshot) stored at ``stored_at``."""
    cutoff = cutoff or DECISION_AT - timedelta(hours=1)
    sign = 1 if direction is Direction.LONG else -1
    e = Decimal(entry)
    with session_scope(factory) as session:
        snapshot = StrategyFeatureSnapshotRecord(
            id=uuid.uuid4(),
            asset_id=asset_id,
            timeframe="1Day",
            data_cutoff=cutoff,
            known_at=cutoff,
            basis=KnowledgeBasis.RECORDED,
            provider="alpaca",
            feed="iex",
            adjustment=PriceAdjustment.SPLIT,
            feature_registry_version="fixture",
            features={},
            feature_count=10,
            valid_count=10,
            inputs_available_at=cutoff,
            last_period_event_at=cutoff - timedelta(hours=5),
            content_sha256=uuid.uuid4().hex * 2,
        )
        evaluation = StrategyEvaluationRecord(
            id=uuid.uuid4(),
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            asset_id=asset_id,
            timeframe="1Day",
            mode=EvaluationMode.LIVE,
            basis=KnowledgeBasis.RECORDED,
            trigger=TriggerKind.BAR_CLOSE,
            trigger_ref=cutoff.date().isoformat(),
            idempotency_key=uuid.uuid4().hex * 2,
            attempt=1,
            status=EvaluationStatus.PROPOSED,
            data_cutoff=cutoff,
            known_at=cutoff,
            worker_id="test",
            started_at=stored_at,
            lease_expires_at=stored_at + timedelta(minutes=5),
            finished_at=stored_at,
            proposals_count=1,
            signal_state=SignalState.SIGNAL,
        )
        session.add_all([snapshot, evaluation])
        session.flush()
        proposal = StrategyProposalRecord(
            id=uuid.uuid4(),
            evaluation_id=evaluation.id,
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            asset_id=asset_id,
            market="EQUITY",
            direction=direction,
            timeframe="1Day",
            mode=EvaluationMode.LIVE,
            basis=KnowledgeBasis.RECORDED,
            signal_at=cutoff,
            data_cutoff=cutoff,
            feature_snapshot_ids=[snapshot.id],
            reference_entry=e,
            invalidation_level=e - sign * Decimal(4),
            target_reference=e + sign * Decimal(10),
            holding_horizon_periods=5,
            signal_strength=Decimal(strength),
            strength_kind=StrengthKind.UNCALIBRATED_SCORE,
            evidence=[],
            reasoning="TEST FIXTURE proposal",
            expires_at=expires_at or DECISION_AT + timedelta(days=3),
            evaluation_event=f"BAR_CLOSE:{uuid.uuid4().hex[:8]}",
            levels={},
            created_at=stored_at,
        )
        session.add(proposal)
        return proposal.id


def make_market_regime(
    factory: sessionmaker[Session], *, label: str, dimension: Dimension, available_at: datetime
) -> int:
    with session_scope(factory) as session:
        config = IntelligenceConfigRecord(
            id=uuid.uuid4(),
            code=f"fixture-{uuid.uuid4().hex[:6]}",
            version=1,
            description="fixture",
            config={},
            content_sha256=uuid.uuid4().hex * 2,
            created_by="test",
        )
        session.add(config)
        session.flush()
        run = IntelligenceRunRecord(
            id=uuid.uuid4(),
            kind=RunKind.LIVE,
            status=RunStatus.COMPLETED,
            basis=KnowledgeBasis.RECORDED,
            provider="alpaca",
            feed="iex",
            adjustment=PriceAdjustment.SPLIT,
            config_id=config.id,
            as_of_start=available_at - timedelta(hours=1),
            as_of_end=available_at - timedelta(hours=1),
            requested_by="test",
            started_at=available_at,
            heartbeat_at=available_at,
            completed_at=available_at,
        )
        session.add(run)
        session.flush()
        row = RegimeClassificationRecord(
            run_id=run.id,
            as_of=available_at - timedelta(hours=1),
            basis=KnowledgeBasis.RECORDED,
            available_at=available_at,
            scope=Scope.MARKET,
            asset_id=None,
            timeframe="1Day",
            dimension=dimension,
            algorithm="fixture",
            config_id=config.id,
            label=label,
            confidence=Decimal("0.7"),
            probabilities={label: "0.7", "UNCERTAIN": "0.3"},
            quality=ClassificationQuality.OK,
        )
        session.add(row)
        session.flush()
        return row.id


__all__ = [
    "DECISION_AT",
    "NOW",
    "make_asset",
    "make_market_regime",
    "make_proposal",
    "make_strategy",
]

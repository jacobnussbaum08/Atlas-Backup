"""The coordinator's inputs as data (Phase 5B).

An :class:`InputBundle` holds every point-in-time value one decision run uses, copied out
of the database by :mod:`~app.ai_engine.coordinator.gather`. Evaluation
(:mod:`~app.ai_engine.coordinator.evaluate`) is a pure function of a bundle and a policy,
and each item is stored in ``decision_inputs`` with its exact payload, so a run is replayed
from stored rows alone, never from a fresh read or a new LLM call.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from app.ai_engine.enums import DecisionMode, InputKind
from app.feature_engine.registry import sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import Direction


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ClassVar[InputKind]

    @property
    def ref(self) -> str:
        raise NotImplementedError

    @property
    def item_asset_id(self) -> uuid.UUID | None:
        return getattr(self, "asset_id", None)

    @property
    def item_as_of(self) -> datetime | None:
        return None

    @property
    def item_available_at(self) -> datetime | None:
        return getattr(self, "available_at", None)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ProposalInput(_Item):
    kind: ClassVar[InputKind] = InputKind.STRATEGY_PROPOSAL
    id: uuid.UUID
    strategy_id: str
    family: str
    strategy_version_id: uuid.UUID
    strategy_version: int
    logic_version: str
    configuration_sha256: str
    asset_id: uuid.UUID
    symbol: str
    market: str
    direction: Direction
    timeframe: str
    mode: str
    basis: KnowledgeBasis
    signal_at: datetime
    data_cutoff: datetime
    # When the proposal row was stored (RECORDED runs read only rows stored by then).
    available_at: datetime
    expires_at: datetime
    reference_entry: Decimal
    invalidation_level: Decimal | None
    target_reference: Decimal | None
    holding_horizon_periods: int
    signal_strength: Decimal
    strength_kind: str
    feature_snapshot_ids: list[uuid.UUID]
    # Point-in-time quality of the feature snapshots behind the proposal.
    snapshot_feature_count: int
    snapshot_valid_count: int
    snapshot_basis: KnowledgeBasis
    inputs_available_at: datetime | None
    evidence_count: int

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_as_of(self) -> datetime | None:
        return self.signal_at


class RecordedConflictInput(_Item):
    kind: ClassVar[InputKind] = InputKind.PROPOSAL_CONFLICT
    id: int
    asset_id: uuid.UUID
    proposal_a_id: uuid.UUID
    proposal_b_id: uuid.UUID
    detected_at: datetime

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_available_at(self) -> datetime | None:
        return self.detected_at


class RegimeInput(_Item):
    kind: ClassVar[InputKind] = InputKind.MARKET_REGIME
    id: int
    scope: str
    asset_id: uuid.UUID | None
    timeframe: str
    dimension: str
    label: str
    confidence: float
    quality: str
    basis: KnowledgeBasis
    as_of: datetime
    available_at: datetime
    algorithm: str

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_as_of(self) -> datetime | None:
        return self.as_of


class AssetRegimeInput(RegimeInput):
    kind: ClassVar[InputKind] = InputKind.ASSET_REGIME


class AssetFeaturesInput(_Item):
    """Phase 3B per-asset features and the Phase 3A liquidity measurement."""

    kind: ClassVar[InputKind] = InputKind.ASSET_FEATURES
    asset_id: uuid.UUID
    timeframe: str
    snapshot_id: int | None
    feature_set_version: str | None
    features: dict[str, float | None]
    quality: str | None
    basis: KnowledgeBasis | None
    as_of: datetime | None
    available_at: datetime | None
    liquidity_metric_id: int | None
    median_dollar_volume: float | None
    liquidity_as_of: datetime | None

    @property
    def ref(self) -> str:
        return f"{self.asset_id}:{self.timeframe}"

    @property
    def item_as_of(self) -> datetime | None:
        return self.as_of


class CorrelationInput(_Item):
    kind: ClassVar[InputKind] = InputKind.CROSS_ASSET
    id: int
    asset_a: uuid.UUID
    asset_b: uuid.UUID
    correlation: float
    window: int
    observations: int
    as_of: datetime
    available_at: datetime

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_asset_id(self) -> uuid.UUID | None:
        return self.asset_a

    @property
    def item_as_of(self) -> datetime | None:
        return self.as_of


class NewsEventsInput(_Item):
    """Phase 3C events directly related to an asset over the look-back window."""

    kind: ClassVar[InputKind] = InputKind.NEWS_EVENTS
    asset_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    event_count: int
    event_ids: list[int]
    categories: dict[str, int]
    latest_event_at: datetime | None
    available_at: datetime | None

    @property
    def ref(self) -> str:
        return f"{self.asset_id}:{self.window_start.isoformat()}"

    @property
    def item_as_of(self) -> datetime | None:
        return self.window_end


class LLMAnalysisInput(_Item):
    kind: ClassVar[InputKind] = InputKind.LLM_ANALYSIS
    id: uuid.UUID
    task: str
    subject_ref: str
    article_id: uuid.UUID | None
    asset_ids: list[uuid.UUID]
    # Asset id -> sentiment in {-1, 0, 1} as the LLM estimated it (unverified).
    sentiment: dict[str, int]
    event_type: str | None
    catalysts: list[str]
    summary: str | None
    provider: str
    model: str
    prompt_version: str
    subject_time: datetime | None
    available_at: datetime

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_asset_id(self) -> uuid.UUID | None:
        return self.asset_ids[0] if self.asset_ids else None

    @property
    def item_as_of(self) -> datetime | None:
        return self.subject_time


class MLPredictionInput(_Item):
    kind: ClassVar[InputKind] = InputKind.ML_PREDICTION
    id: uuid.UUID
    model_version_id: uuid.UUID
    model_name: str
    model_version: int
    model_type: str
    # Lifecycle status at the decision time (from the status events stored by then).
    status_at_decision: str
    mode: str
    asset_id: uuid.UUID | None
    direction: Direction
    timeframe: str
    strategy_id: str | None
    decision_at: datetime
    predicted_at: datetime
    output_kind: str
    output: dict[str, Any]
    feature_snapshot_sha256: str
    validation_status: str
    target_kind: str
    barrier_source: str | None
    horizon_periods: int | None
    calibration: dict[str, Any] | None
    test_metrics: dict[str, Any]
    test_baseline: dict[str, Any]
    model_created_at: datetime

    @property
    def ref(self) -> str:
        return str(self.id)

    @property
    def item_as_of(self) -> datetime | None:
        return self.decision_at

    @property
    def item_available_at(self) -> datetime | None:
        return self.predicted_at


class TrackRecordInput(_Item):
    """Realized net outcomes of a strategy's past signals from a stored ML dataset version
    (STRATEGY_REPLAY or STRATEGY_PROPOSALS) known at the decision time."""

    kind: ClassVar[InputKind] = InputKind.STRATEGY_TRACK_RECORD
    strategy_id: str
    direction: Direction
    timeframe: str
    dataset_version_id: uuid.UUID
    dataset_source: str
    data_known_at: datetime
    observations: int
    mean_net_return: float | None
    hit_rate: float | None
    cost_assumption_bps: float | None
    last_label_end_at: datetime | None

    @property
    def ref(self) -> str:
        return f"{self.strategy_id}:{self.direction.value}:{self.timeframe}"

    @property
    def item_available_at(self) -> datetime | None:
        return self.data_known_at


ITEM_TYPES: dict[InputKind, type[_Item]] = {
    InputKind.STRATEGY_PROPOSAL: ProposalInput,
    InputKind.PROPOSAL_CONFLICT: RecordedConflictInput,
    InputKind.MARKET_REGIME: RegimeInput,
    InputKind.ASSET_REGIME: AssetRegimeInput,
    InputKind.ASSET_FEATURES: AssetFeaturesInput,
    InputKind.CROSS_ASSET: CorrelationInput,
    InputKind.NEWS_EVENTS: NewsEventsInput,
    InputKind.LLM_ANALYSIS: LLMAnalysisInput,
    InputKind.ML_PREDICTION: MLPredictionInput,
    InputKind.STRATEGY_TRACK_RECORD: TrackRecordInput,
}


class InputBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_at: datetime
    knowledge_at: datetime
    basis: KnowledgeBasis
    mode: DecisionMode
    proposals: list[ProposalInput] = []
    recorded_conflicts: list[RecordedConflictInput] = []
    market_regimes: list[RegimeInput] = []
    asset_regimes: list[AssetRegimeInput] = []
    asset_features: list[AssetFeaturesInput] = []
    correlations: list[CorrelationInput] = []
    news: list[NewsEventsInput] = []
    llm_analyses: list[LLMAnalysisInput] = []
    ml_predictions: list[MLPredictionInput] = []
    track_records: list[TrackRecordInput] = []
    # What could not be read and why (e.g. no market intelligence run yet).
    notes: list[str] = []

    def items(self) -> list[_Item]:
        out: list[_Item] = []
        for group in (
            self.proposals,
            self.recorded_conflicts,
            self.market_regimes,
            self.asset_regimes,
            self.asset_features,
            self.correlations,
            self.news,
            self.llm_analyses,
            self.ml_predictions,
            self.track_records,
        ):
            out.extend(group)
        return out

    def sha256(self) -> str:
        return sha256_json(
            {
                "decision_at": self.decision_at.isoformat(),
                "knowledge_at": self.knowledge_at.isoformat(),
                "basis": self.basis.value,
                "mode": self.mode.value,
                "items": sorted(
                    (item.kind.value, item.ref, sha256_json(item.payload()))
                    for item in self.items()
                ),
                "notes": self.notes,
            }
        )

    @classmethod
    def from_rows(
        cls,
        *,
        decision_at: datetime,
        knowledge_at: datetime,
        basis: KnowledgeBasis,
        mode: DecisionMode,
        rows: list[tuple[InputKind, dict[str, Any]]],
        notes: list[str],
    ) -> InputBundle:
        """Rebuild a bundle from stored ``decision_inputs`` payloads (replay)."""
        groups: dict[InputKind, list[Any]] = {kind: [] for kind in ITEM_TYPES}
        for kind, payload in rows:
            groups[kind].append(ITEM_TYPES[kind].model_validate(payload))
        return cls(
            decision_at=decision_at,
            knowledge_at=knowledge_at,
            basis=basis,
            mode=mode,
            proposals=sorted(groups[InputKind.STRATEGY_PROPOSAL], key=lambda p: str(p.id)),
            recorded_conflicts=sorted(groups[InputKind.PROPOSAL_CONFLICT], key=lambda c: c.id),
            market_regimes=sorted(groups[InputKind.MARKET_REGIME], key=lambda r: r.id),
            asset_regimes=sorted(groups[InputKind.ASSET_REGIME], key=lambda r: r.id),
            asset_features=sorted(groups[InputKind.ASSET_FEATURES], key=lambda f: f.ref),
            correlations=sorted(groups[InputKind.CROSS_ASSET], key=lambda c: c.id),
            news=sorted(groups[InputKind.NEWS_EVENTS], key=lambda n: n.ref),
            llm_analyses=sorted(groups[InputKind.LLM_ANALYSIS], key=lambda a: str(a.id)),
            ml_predictions=sorted(groups[InputKind.ML_PREDICTION], key=lambda m: str(m.id)),
            track_records=sorted(groups[InputKind.STRATEGY_TRACK_RECORD], key=lambda t: t.ref),
            notes=notes,
        )


__all__ = [
    "ITEM_TYPES",
    "AssetFeaturesInput",
    "AssetRegimeInput",
    "CorrelationInput",
    "InputBundle",
    "LLMAnalysisInput",
    "MLPredictionInput",
    "NewsEventsInput",
    "ProposalInput",
    "RecordedConflictInput",
    "RegimeInput",
    "TrackRecordInput",
]

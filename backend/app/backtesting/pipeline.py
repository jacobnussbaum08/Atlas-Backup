"""The live decision pipeline's coordinator stage, replayed (D-234).

Live trading (10A) runs: strategies -> Phase 3B market intelligence -> Phase 5B coordinator
-> only PRIORITIZE_FOR_REVIEW decisions -> 6B risk (with the decision's lead proposal) ->
execution. The 8A replay sends every strategy proposal straight to risk. With
``pipeline.coordinator`` the replay inserts the coordinator, calling the same pure
functions live trading calls:

* :func:`app.market_intelligence.engine.compute` on the replay's own point-in-time bars
  (split-adjusted as known at the decision) for the benchmark and the universe, with the
  default intelligence configuration: market and asset trend, volatility and stability
  regimes and asset features. The market volatility regime is the one live trading
  defers on (``defer_market_volatility = ("EXTREME",)``);
* :func:`app.ai_engine.coordinator.evaluate.evaluate` with the default decision policy,
  unchanged.

Inputs live trading has and the replay does not are left out, never invented, and
reported: ML predictions (no approved model), LLM news analyses, strategy track records
(none stored), recorded conflicts. Everything the replay computes is labelled
RECONSTRUCTED, which the coordinator itself treats more cautiously than RECORDED inputs
(reliability x0.8, +0.1 uncertainty). Universe membership for participation is the
replay's universe, not the live qualified universe.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.ai_engine.coordinator.bundle import (
    AssetFeaturesInput,
    AssetRegimeInput,
    CorrelationInput,
    InputBundle,
    ProposalInput,
    RegimeInput,
)
from app.ai_engine.coordinator.evaluate import evaluate
from app.ai_engine.coordinator.policy import DecisionPolicy
from app.ai_engine.enums import DecisionMode, DecisionOutcome
from app.asset_discovery.sessions import completed_sessions
from app.feature_engine.features import Bar
from app.market_intelligence.config import DEFAULT_CONFIGS, IntelligenceConfig
from app.market_intelligence.engine import AssetInput, IntelligenceResult, compute
from app.market_intelligence.enums import KnowledgeBasis

NAMESPACE = uuid.UUID("5f0c8a52-77c1-4a63-9d0b-2b2a6f6b2a34")
PRIORITIZED = DecisionOutcome.PRIORITIZE_FOR_REVIEW
OMITTED_INPUTS = (
    "ML predictions: no approved model in the replay",
    "LLM news analyses: none historically",
    "strategy track records: none stored (live has none either today)",
    "recorded proposal conflicts: not recorded by the replay",
)


def default_intelligence_config() -> IntelligenceConfig:
    return next(iter(DEFAULT_CONFIGS.values()))[1]


def sessions_needed(config: IntelligenceConfig) -> int:
    """As the live service (``MarketIntelligenceService._sessions_needed``)."""
    daily = config.spec("1Day")
    need = daily.window + daily.change_lookback + 1
    need = max(need, config.correlation.window + config.correlation.prior_window + 1)
    for spec in config.timeframes:
        if spec.timeframe != "1Day":
            need = max(need, (spec.window + spec.change_lookback + 2) * 5 + 10)
    return need


@dataclass
class Draft:
    """One strategy proposal before the coordinator, with what it needs to be described."""

    key: tuple[Any, ...]
    prepared: Any  # engine._PreparedStrategy
    asset_id: uuid.UUID
    spec: Any  # strategies ProposalSpec
    feature_sha: str
    inputs: Any  # strategies FeatureInputs
    proposal_id: uuid.UUID


@dataclass
class StageResult:
    selected: list[tuple[Draft, datetime]]  # with the decision's expiry
    declined: list[tuple[Draft, str, str, str]]  # draft, outcome, code, message
    market_volatility: str | None
    market_trend: str | None
    outcomes: Counter[str]


class CoordinatorStage:
    def __init__(self, engine: Any, policy: DecisionPolicy | None = None):
        self.engine = engine
        self.policy = policy or DecisionPolicy()
        self.config = default_intelligence_config()
        self.needed = sessions_needed(self.config)
        self._ids = 0
        self.runs = 0
        self.outcomes: Counter[str] = Counter()
        self.codes: Counter[str] = Counter()
        self.volatility_days: Counter[str] = Counter()

    def _id(self) -> int:
        self._ids += 1
        return self._ids

    # ------------------------------------------------------------ intelligence
    def intelligence(self, t: datetime, assets: list[uuid.UUID]) -> IntelligenceResult:
        e = self.engine
        sessions = [
            s
            for s in completed_sessions(e.calendar, t, self.needed, e.settle)
            if s.day >= e.dataset.first_session  # history before the data: missing, not guessed
        ]
        bench = None if e.config.benchmark is None else uuid.UUID(e.config.benchmark.asset_id)
        ids = list(dict.fromkeys(([bench] if bench else []) + assets))
        inputs: dict[uuid.UUID, AssetInput] = {}
        for asset_id in ids:
            try:
                rows = e.bars(
                    e.session,
                    asset_id,
                    sessions,
                    data_cutoff=t,
                    known_at=e.config.known_at,
                    series=e.series,
                )
            except ValueError:
                rows = []  # before the loaded range: missing data, never guessed
            bars = {
                r.session: Bar(
                    event_time=r.event_time,
                    open=r.open,
                    high=r.high,
                    low=r.low,
                    close=r.close,
                    volume=r.volume,
                    available_at=r.available_at,
                )
                for r in rows
                if r.quality_status == "VALID" and r.available_at is not None
            }
            symbol = e.symbol_of(asset_id)
            inputs[asset_id] = AssetInput(asset_id, symbol, bars)
        return compute(
            as_of=t,
            calendar=e.calendar,
            sessions=sessions,
            inputs=inputs,
            benchmark_id=bench,
            members=assets,
            correlation_assets=assets,
            config=self.config,
            feed=e.config.data.feed,
            single_venue=e.config.data.feed == "iex",
        )

    # ------------------------------------------------------------ bundle
    def bundle(
        self, t: datetime, day: date, drafts: list[Draft], intel: IntelligenceResult
    ) -> InputBundle:
        e = self.engine
        proposals = []
        for d in drafts:
            rs, spec, values = d.prepared.resolved, d.spec, d.inputs.values
            proposals.append(
                ProposalInput(
                    id=d.proposal_id,
                    strategy_id=rs.strategy_id,
                    family=rs.family,
                    strategy_version_id=uuid.UUID(rs.version_id),
                    strategy_version=rs.version,
                    logic_version=rs.logic_version,
                    configuration_sha256=rs.parameters_sha256,
                    asset_id=d.asset_id,
                    symbol=e.symbol_of(d.asset_id),
                    market=spec.market,
                    direction=spec.direction,
                    timeframe=spec.timeframe,
                    mode="HISTORICAL",
                    basis=KnowledgeBasis.RECONSTRUCTED,
                    signal_at=t,
                    data_cutoff=t,
                    available_at=t,
                    expires_at=spec.expires_at,
                    reference_entry=spec.reference_entry,
                    invalidation_level=spec.invalidation_level,
                    target_reference=spec.target_reference,
                    holding_horizon_periods=spec.holding_horizon_periods,
                    signal_strength=spec.signal_strength,
                    strength_kind=spec.strength_kind.value,
                    feature_snapshot_ids=[],
                    snapshot_feature_count=len(values),
                    snapshot_valid_count=sum(1 for v in values.values() if v.valid),
                    snapshot_basis=KnowledgeBasis.RECONSTRUCTED,
                    inputs_available_at=d.inputs.inputs_available_at,
                    evidence_count=len(spec.evidence),
                )
            )
        market: list[RegimeInput] = []
        for timeframe, classes in sorted(intel.market.items()):
            for c in classes:
                market.append(self._regime(RegimeInput, "MARKET", None, timeframe, c, t))
        asset_regimes: list[AssetRegimeInput] = []
        features: list[AssetFeaturesInput] = []
        wanted = {d.asset_id for d in drafts}
        for item in intel.assets:
            if item.asset_id not in wanted:
                continue
            for c in (item.trend, item.volatility):
                asset_regimes.append(
                    self._regime(AssetRegimeInput, "ASSET", item.asset_id, item.timeframe, c, t)
                )
            adv, _ = e.liquidity_at(item.asset_id, day, t)
            f = item.features
            features.append(
                AssetFeaturesInput(
                    asset_id=item.asset_id,
                    timeframe=item.timeframe,
                    snapshot_id=None,
                    feature_set_version="replay",
                    features={k: v for k, v in sorted(f.values.items())},
                    quality=f.quality.value,
                    basis=KnowledgeBasis.RECONSTRUCTED,
                    as_of=t,
                    available_at=t,
                    liquidity_metric_id=None,
                    median_dollar_volume=None if adv is None else float(adv),
                    liquidity_as_of=None if adv is None else t,
                )
            )
        correlations: list[CorrelationInput] = []
        if intel.cross_asset is not None:
            for p in intel.cross_asset.pairs:
                if p.correlation is None:
                    continue
                if p.asset_a not in wanted and p.asset_b not in wanted:
                    continue
                correlations.append(
                    CorrelationInput(
                        id=self._id(),
                        asset_a=p.asset_a,
                        asset_b=p.asset_b,
                        correlation=float(p.correlation),
                        window=self.config.correlation.window,
                        observations=p.observations,
                        as_of=t,
                        available_at=t,
                    )
                )
        return InputBundle(
            decision_at=t,
            knowledge_at=t,
            basis=KnowledgeBasis.RECONSTRUCTED,
            mode=DecisionMode.HISTORICAL,
            proposals=sorted(proposals, key=lambda p: str(p.id)),
            market_regimes=market,
            asset_regimes=asset_regimes,
            asset_features=features,
            correlations=correlations,
            notes=list(OMITTED_INPUTS),
        )

    def _regime(
        self,
        cls: type[RegimeInput],
        scope: str,
        asset_id: uuid.UUID | None,
        timeframe: str,
        c: Any,
        t: datetime,
    ) -> Any:
        return cls(
            id=self._id(),
            scope=scope,
            asset_id=asset_id,
            timeframe=timeframe,
            dimension=c.dimension.value,
            label=c.label,
            confidence=float(c.confidence),
            quality=c.quality.value,
            basis=KnowledgeBasis.RECONSTRUCTED,
            as_of=t,
            available_at=t,
            algorithm=c.algorithm,
        )

    # ------------------------------------------------------------ stage
    def run(self, t: datetime, day: date, drafts: list[Draft]) -> StageResult:
        assets = sorted({d.asset_id for d in drafts} | set(self.engine.universe), key=str)
        intel = self.intelligence(t, assets)
        bundle = self.bundle(t, day, drafts, intel)
        evaluation = evaluate(bundle, self.policy)
        self.runs += 1
        by_id = {d.proposal_id: d for d in drafts}
        selected: list[tuple[Draft, datetime]] = []
        declined: list[tuple[Draft, str, str, str]] = []
        placed: set[uuid.UUID] = set()
        outcomes: Counter[str] = Counter()
        for decision in evaluation.decisions:
            if decision.asset_id is None:
                continue  # the run-level NO_OPPORTUNITY record
            outcomes[decision.outcome.value] += 1
            reason = decision.reasons[0] if decision.reasons else {}
            code = str(reason.get("code") or decision.outcome.value)
            message = str(reason.get("message") or decision.explanation)[:500]
            lead = (decision.expected or {}).get("lead_proposal_id")
            for pid in decision.proposal_ids:
                draft = by_id.get(pid)
                if draft is None:
                    continue
                placed.add(pid)
                if decision.outcome is PRIORITIZED and lead is not None and str(pid) == str(lead):
                    selected.append((draft, decision.expires_at))
                elif decision.outcome is PRIORITIZED:
                    declined.append((draft, "MERGED", "MERGED_INTO_LEAD", "not the lead proposal"))
                else:
                    declined.append((draft, decision.outcome.value, code, message))
                    self.codes[code] += 1
        for d in drafts:
            if d.proposal_id not in placed:
                declined.append((d, "UNPLACED", "NOT_IN_DECISION", "no decision named it"))
        self.outcomes.update(outcomes)
        daily = {r.dimension: r.label for r in bundle.market_regimes if r.timeframe == "1Day"}
        vol, trend = daily.get("VOLATILITY"), daily.get("TREND")
        self.volatility_days[str(vol)] += 1
        return StageResult(selected, declined, vol, trend, outcomes)

    def summary(self) -> dict[str, Any]:
        return {
            "coordinator_runs": self.runs,
            "decision_outcomes": dict(sorted(self.outcomes.items())),
            "decline_codes": dict(self.codes.most_common()),
            "market_volatility_on_decision_days": dict(sorted(self.volatility_days.items())),
            "policy_sha256": self.policy.sha256(),
            "omitted_inputs": list(OMITTED_INPUTS),
        }


def expiry_cap(decision_expires: datetime, proposal_expires: datetime) -> datetime:
    return min(decision_expires, proposal_expires)


__all__ = ["CoordinatorStage", "Draft", "StageResult", "expiry_cap", "timedelta"]

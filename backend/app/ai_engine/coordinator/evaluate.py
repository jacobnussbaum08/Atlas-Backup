"""Decision evaluation: a pure function of an input bundle and a policy (Phase 5B).

Nothing here reads the database, the clock or an LLM: the same bundle and policy always
give the same decisions, which is what makes a stored run replayable.

**Opportunities.** Proposals are grouped per asset and direction. Several timeframes or
repeated proposals of one strategy count once; proposals of different strategies with
nearly identical entries are one opportunity (recorded as DUPLICATE_PROPOSALS) and their
agreement is evidence. Opposite directions on one asset are *separate* opportunities that
compete: consensus is never forced.

**Components** (each a documented [0, 1] heuristic, or UNAVAILABLE / EXCLUDED with the
reason): see :data:`~app.ai_engine.coordinator.policy.COMPONENTS`. The heuristic score is
their weighted mean over the available ones; the ranking score discounts it for
uncertainty and conflicts. **Neither is a probability.** A calibrated probability is
reported separately, only from an APPROVED model, with what it is a probability of.

**Outcomes**, in order (the first rule that applies wins; every applied rule is a reason):

1. INSUFFICIENT_DATA: too little of the weighted evidence existed, or the data behind the
   proposals is unreliable.
2. REJECT: net reward/risk below the floor; a supported expected value <= 0; a market trend
   regime opposing the strategy family (policy).
3. DEFER: stale inputs; a deferring market volatility regime; uncertainty above the cap;
   disagreeing approved models; opposite-direction opportunities too close to call.
4. PRIORITIZE_FOR_REVIEW: ranking score at or above the threshold, not out-ranked by a
   stronger opposing or correlated opportunity, within the per-run cap.
5. DEPRIORITIZE: everything else that was scored.

A run with nothing prioritized also records NO_OPPORTUNITY. A prioritized opportunity is a
candidate for the future risk engine (Phase 6). It is not an approved trade.
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.ai_engine.coordinator.bundle import (
    AssetFeaturesInput,
    InputBundle,
    LLMAnalysisInput,
    MLPredictionInput,
    ProposalInput,
    RegimeInput,
)
from app.ai_engine.coordinator.expected_value import (
    ExpectedValue,
    TradeGeometry,
    estimate,
    geometry,
)
from app.ai_engine.coordinator.policy import LOGIC_VERSION, DecisionPolicy
from app.ai_engine.enums import (
    ComponentKind,
    ComponentStatus,
    ConflictType,
    DecisionOutcome,
    EstimateStatus,
    EvidenceStance,
    InputKind,
    RunOutcome,
)
from app.feature_engine.registry import sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import Direction

SCORE_NOTE = (
    "heuristic_score and ranking_score are weighted heuristics that order opportunities; "
    "they are not probabilities of profit"
)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sign(direction: Direction) -> int:
    return 1 if direction is Direction.LONG else -1


def _opposite(direction: Direction) -> Direction:
    return Direction.SHORT if direction is Direction.LONG else Direction.LONG


@dataclass
class Component:
    name: str
    kind: ComponentKind
    status: ComponentStatus
    score: float | None
    raw_value: float | None
    reason: str
    inputs: dict[str, Any] = field(default_factory=dict)
    weight: float = 0.0
    contribution: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "score": self.score,
            "raw_value": self.raw_value,
            "weight": self.weight,
            "contribution": self.contribution,
            "reason": self.reason,
            "inputs": self.inputs,
        }


def _available(
    name: str, score: float, raw: float | None, reason: str, kind: ComponentKind, **inputs: Any
) -> Component:
    return Component(name, kind, ComponentStatus.AVAILABLE, clamp(score), raw, reason, dict(inputs))


def _missing(name: str, reason: str, *, excluded: bool = False, **inputs: Any) -> Component:
    status = ComponentStatus.EXCLUDED if excluded else ComponentStatus.UNAVAILABLE
    return Component(name, ComponentKind.HEURISTIC, status, None, None, reason, dict(inputs))


@dataclass
class Evidence:
    kind: InputKind
    ref: str | None
    stance: EvidenceStance
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "ref": self.ref,
            "stance": self.stance.value,
            "summary": self.summary,
            "data": self.data,
        }


@dataclass
class Reason:
    code: str
    message: str
    outcome: DecisionOutcome | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "outcome": None if self.outcome is None else self.outcome.value,
        }


@dataclass
class ConflictDraft:
    type: ConflictType
    asset_id: uuid.UUID | None
    keys: list[tuple[uuid.UUID, Direction]]
    proposal_ids: list[uuid.UUID]
    handling: str
    description: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "asset_id": None if self.asset_id is None else str(self.asset_id),
            "opportunities": [f"{a}:{d.value}" for a, d in self.keys],
            "proposal_ids": [str(p) for p in self.proposal_ids],
            "handling": self.handling,
            "description": self.description,
            "data": self.data,
        }


@dataclass
class Candidate:
    asset_id: uuid.UUID
    symbol: str
    direction: Direction
    proposals: list[ProposalInput]
    # Strategy id -> its representative proposal (strongest, then newest).
    by_strategy: dict[str, ProposalInput]
    lead: ProposalInput
    components: list[Component] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)
    conflicts: list[ConflictDraft] = field(default_factory=list)
    geometry: TradeGeometry | None = None
    expected_value: ExpectedValue | None = None
    usable_ml: list[MLPredictionInput] = field(default_factory=list)
    ml_disagreement: float = 0.0
    heuristic: float | None = None
    coverage: float = 0.0
    uncertainty: dict[str, Any] = field(default_factory=dict)
    ranking: float | None = None
    outcome: DecisionOutcome | None = None
    rank: int | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[uuid.UUID, Direction]:
        return (self.asset_id, self.direction)

    @property
    def timeframes(self) -> list[str]:
        return sorted({p.timeframe for p in self.proposals})

    def component(self, name: str) -> Component | None:
        return next((c for c in self.components if c.name == name), None)

    def decide(self, outcome: DecisionOutcome, code: str, message: str) -> None:
        self.reasons.append(Reason(code, message, outcome))
        if self.outcome is None:
            self.outcome = outcome


@dataclass
class Decision:
    """A decision record as data (persisted by ``store``; compared by replay)."""

    outcome: DecisionOutcome
    rank: int | None
    asset_id: uuid.UUID | None
    symbol: str | None
    direction: Direction | None
    timeframes: list[str]
    proposal_ids: list[uuid.UUID]
    strategy_versions: list[dict[str, Any]]
    model_versions: list[dict[str, Any]]
    regime_snapshot: dict[str, Any]
    news_refs: list[dict[str, Any]]
    llm_refs: list[dict[str, Any]]
    feature_timestamps: dict[str, Any]
    scores: dict[str, Any]
    expected: dict[str, Any]
    uncertainty: dict[str, Any]
    conflicts: list[dict[str, Any]]
    reasons: list[dict[str, Any]]
    explanation: str
    decision_at: datetime
    data_cutoff: datetime
    expires_at: datetime
    components: list[dict[str, Any]]
    evidence: list[dict[str, Any]]

    def content(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "rank": self.rank,
            "asset_id": None if self.asset_id is None else str(self.asset_id),
            "symbol": self.symbol,
            "direction": None if self.direction is None else self.direction.value,
            "timeframes": self.timeframes,
            "proposal_ids": [str(p) for p in self.proposal_ids],
            "strategy_versions": self.strategy_versions,
            "model_versions": self.model_versions,
            "regime_snapshot": self.regime_snapshot,
            "news_refs": self.news_refs,
            "llm_refs": self.llm_refs,
            "feature_timestamps": self.feature_timestamps,
            "scores": self.scores,
            "expected": self.expected,
            "uncertainty": self.uncertainty,
            "conflicts": self.conflicts,
            "reasons": self.reasons,
            "explanation": self.explanation,
            "decision_at": self.decision_at.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "components": self.components,
            "evidence": self.evidence,
        }

    def sha256(self) -> str:
        return sha256_json(self.content())


@dataclass
class Evaluation:
    decisions: list[Decision]
    conflicts: list[ConflictDraft]
    outcome: RunOutcome
    proposals_considered: int
    summary: dict[str, Any]

    def result_sha256(self) -> str:
        return sha256_json(
            {
                "logic_version": LOGIC_VERSION,
                "outcome": self.outcome.value,
                "decisions": sorted(d.sha256() for d in self.decisions),
                "conflicts": sorted(sha256_json(c.to_json()) for c in self.conflicts),
            }
        )


# --- Context lookups -------------------------------------------------------------------


class _Context:
    def __init__(self, bundle: InputBundle) -> None:
        self.bundle = bundle
        self.market: dict[tuple[str, str], RegimeInput] = {}
        for r in sorted(bundle.market_regimes, key=lambda r: (r.as_of, r.available_at, r.id)):
            self.market[(r.timeframe, r.dimension)] = r
        self.asset_regimes: dict[tuple[uuid.UUID, str, str], RegimeInput] = {}
        for a in sorted(bundle.asset_regimes, key=lambda r: (r.as_of, r.available_at, r.id)):
            if a.asset_id is not None:
                self.asset_regimes[(a.asset_id, a.timeframe, a.dimension)] = a
        self.features = {(f.asset_id, f.timeframe): f for f in bundle.asset_features}
        self.correlations: dict[frozenset[uuid.UUID], float] = {}
        self.correlation_refs: dict[frozenset[uuid.UUID], int] = {}
        for c in sorted(bundle.correlations, key=lambda c: (c.as_of, c.available_at, c.id)):
            pair = frozenset((c.asset_a, c.asset_b))
            self.correlations[pair] = c.correlation
            self.correlation_refs[pair] = c.id
        self.news = {n.asset_id: n for n in bundle.news}
        self.track = {(t.strategy_id, t.direction, t.timeframe): t for t in bundle.track_records}

    def market_regime(self, timeframe: str, dimension: str) -> RegimeInput | None:
        return self.market.get((timeframe, dimension)) or self.market.get(("1Day", dimension))

    def asset_regime(self, asset_id: uuid.UUID, timeframe: str, dim: str) -> RegimeInput | None:
        return self.asset_regimes.get((asset_id, timeframe, dim)) or self.asset_regimes.get(
            (asset_id, "1Day", dim)
        )

    def asset_features(self, asset_id: uuid.UUID, timeframe: str) -> AssetFeaturesInput | None:
        return self.features.get((asset_id, timeframe)) or self.features.get((asset_id, "1Day"))


def _relation(label: str, direction: Direction) -> str | None:
    if label == "SIDEWAYS":
        return "sideways"
    if label == "TRANSITIONING":
        return "transitioning"
    if label in ("BULLISH", "BEARISH"):
        aligned = (label == "BULLISH") == (direction is Direction.LONG)
        return "aligned" if aligned else "opposed"
    return None  # UNCERTAIN or unknown


# --- Grouping --------------------------------------------------------------------------


def _strength_key(p: ProposalInput) -> tuple[Any, ...]:
    return (p.signal_strength, p.signal_at, str(p.id))


def group(
    bundle: InputBundle, policy: DecisionPolicy
) -> tuple[list[Candidate], list[ConflictDraft]]:
    by_key: dict[tuple[uuid.UUID, Direction], list[ProposalInput]] = defaultdict(list)
    for p in sorted(bundle.proposals, key=lambda p: str(p.id)):
        by_key[(p.asset_id, p.direction)].append(p)
    candidates: list[Candidate] = []
    conflicts: list[ConflictDraft] = []
    for (asset_id, direction), proposals in sorted(
        by_key.items(), key=lambda kv: (str(kv[0][0]), kv[0][1].value)
    ):
        by_strategy: dict[str, ProposalInput] = {}
        for p in proposals:
            best = by_strategy.get(p.strategy_id)
            if best is None or _strength_key(p) > _strength_key(best):
                by_strategy[p.strategy_id] = p
        lead = max(by_strategy.values(), key=_strength_key)
        candidate = Candidate(
            asset_id=asset_id,
            symbol=lead.symbol,
            direction=direction,
            proposals=proposals,
            by_strategy=dict(sorted(by_strategy.items())),
            lead=lead,
        )
        repeats = [p for p in proposals if by_strategy[p.strategy_id].id != p.id]
        reps = list(candidate.by_strategy.values())
        near = sorted(
            {
                str(p.id)
                for p in reps
                for q in reps
                if p.id != q.id
                and abs(float(p.reference_entry) - float(q.reference_entry))
                <= policy.duplicate_entry_tolerance * float(q.reference_entry)
            }
        )
        if repeats or near:
            parts = []
            if near:
                parts.append(
                    f"{len(near)} proposals of different strategies with entries within "
                    f"{policy.duplicate_entry_tolerance:.1%} of each other"
                )
            if repeats:
                parts.append(
                    f"{len(repeats)} further proposal(s) of a strategy already counted "
                    "(other timeframe or repeat)"
                )
            draft = ConflictDraft(
                type=ConflictType.DUPLICATE_PROPOSALS,
                asset_id=asset_id,
                keys=[candidate.key],
                proposal_ids=sorted({p.id for p in proposals}, key=str),
                handling="merged into one opportunity; each strategy counted once for "
                "agreement and the opportunity once for review capacity",
                description="; ".join(parts),
                data={"near_identical": near, "repeats": sorted(str(p.id) for p in repeats)},
            )
            candidate.conflicts.append(draft)
            conflicts.append(draft)
        candidates.append(candidate)
    return candidates, conflicts


# --- Components ------------------------------------------------------------------------


def _signal_quality(c: Candidate) -> Component:
    strengths = [float(p.signal_strength) for p in c.by_strategy.values()]
    mean = statistics.fmean(strengths)
    return _available(
        "signal_quality",
        mean,
        mean,
        f"mean uncalibrated signal strength of {len(strengths)} strategy proposal(s)",
        ComponentKind.HEURISTIC,
        strengths={k: float(p.signal_strength) for k, p in c.by_strategy.items()},
    )


def _agreement(c: Candidate, opposing: set[str], policy: DecisionPolicy) -> Component:
    n_agree, n_opp = len(c.by_strategy), len(opposing)
    share = n_agree / (n_agree + n_opp)
    score = share * min(1.0, n_agree / policy.agreement_saturation)
    return _available(
        "strategy_agreement",
        score,
        float(n_agree - n_opp),
        f"{n_agree} strategy(ies) propose {c.direction.value}, {n_opp} the opposite",
        ComponentKind.HEURISTIC,
        agreeing=sorted(c.by_strategy),
        opposing=sorted(opposing),
    )


def _track_record(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    scores: list[float] = []
    means: list[float] = []
    used: dict[str, Any] = {}
    short: list[str] = []
    for strategy_id, p in c.by_strategy.items():
        record = ctx.track.get((strategy_id, c.direction, p.timeframe))
        if (
            record is None
            or record.mean_net_return is None
            or record.observations < policy.min_track_record
        ):
            short.append(strategy_id)
            continue
        scores.append(clamp(0.5 + 0.5 * record.mean_net_return / policy.track_record_scale))
        means.append(record.mean_net_return)
        used[strategy_id] = {
            "observations": record.observations,
            "mean_net_return": record.mean_net_return,
            "hit_rate": record.hit_rate,
            "dataset_version_id": str(record.dataset_version_id),
            "data_known_at": record.data_known_at.isoformat(),
        }
    if not scores:
        return _missing(
            "track_record",
            f"no stored track record with >= {policy.min_track_record} matured outcomes known "
            "at the decision time (no strategy has been traded or backtested)",
            strategies=short,
        )
    mean_net = statistics.fmean(means)
    return _available(
        "track_record",
        statistics.fmean(scores),
        mean_net,
        "realized net outcomes of past signals from a stored dataset (RECONSTRUCTED replay, "
        "assumed costs): research evidence, not traded performance",
        ComponentKind.HEURISTIC,
        records=used,
        insufficient=short,
    )


def _degraded(p: MLPredictionInput) -> str | None:
    test, base = p.test_metrics, p.test_baseline
    for metric in ("log_loss", "rmse"):
        value, baseline = test.get(metric), base.get(metric)
        if isinstance(value, int | float) and isinstance(baseline, int | float):
            if value > baseline:
                return f"test {metric} {value:.4f} is worse than its baseline {baseline:.4f}"
            return None
    return "no comparable test metric against a baseline was recorded"


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _ml_score(p: MLPredictionInput, policy: DecisionPolicy) -> tuple[float, float, str] | None:
    """(score, raw, kind) for a usable prediction."""
    out = p.output
    probability, raw, est = (
        number(out.get("probability")),
        number(out.get("raw_score")),
        number(out.get("estimate")),
    )
    if p.output_kind == "CALIBRATED_PROBABILITY" and probability is not None:
        return clamp(probability), probability if raw is None else raw, p.output_kind
    if p.output_kind == "UNCALIBRATED_SCORE" and raw is not None:
        return clamp(0.5 + (raw - 0.5) * policy.uncalibrated_authority), raw, p.output_kind
    if p.output_kind == "REGRESSION_ESTIMATE" and est is not None:
        base = clamp(0.5 + 0.5 * est / policy.regression_scale)
        return 0.5 + (base - 0.5) * policy.uncalibrated_authority, est, p.output_kind
    return None


def _ml_support(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    relevant = [
        p
        for p in ctx.bundle.ml_predictions
        if p.asset_id == c.asset_id and p.direction is c.direction and p.timeframe in c.timeframes
    ]
    scored: list[tuple[MLPredictionInput, float, float, str]] = []
    for p in sorted(relevant, key=lambda p: (p.model_name, p.model_version, str(p.id))):
        note = None
        if p.status_at_decision == "SHADOW":
            c.evidence.append(
                Evidence(
                    InputKind.ML_PREDICTION,
                    str(p.id),
                    EvidenceStance.CONTEXT,
                    f"shadow model {p.model_name} v{p.model_version}: recorded, never used",
                    {"output": p.output},
                )
            )
            continue
        if p.status_at_decision != "APPROVED":
            note = f"model status {p.status_at_decision} at the decision time"
        elif p.validation_status != "PASSED":
            note = f"validation status {p.validation_status}"
        else:
            note = _degraded(p)
        result = None if note else _ml_score(p, policy)
        if result is None:
            c.evidence.append(
                Evidence(
                    InputKind.ML_PREDICTION,
                    str(p.id),
                    EvidenceStance.CONTEXT,
                    f"{p.model_name} v{p.model_version} excluded: {note or 'no usable output'}",
                )
            )
            continue
        scored.append((p, *result))
    if not scored:
        return _missing(
            "ml_support",
            "no APPROVED, validated and non-degraded model prediction for this opportunity "
            "was recorded by the decision time",
            predictions_seen=len(relevant),
        )
    c.usable_ml = [s[0] for s in scored]
    values = [s[1] for s in scored]
    half = policy.ml_disagreement_margin / 2
    if len(values) > 1 and min(values) < 0.5 - half and max(values) > 0.5 + half:
        c.ml_disagreement = max(values) - min(values)
    for p, score, _, _ in scored:
        stance = EvidenceStance.SUPPORTS if score > 0.5 else EvidenceStance.OPPOSES
        c.evidence.append(
            Evidence(
                InputKind.ML_PREDICTION,
                str(p.id),
                stance if score != 0.5 else EvidenceStance.NEUTRAL,
                f"{p.model_name} v{p.model_version} ({p.output_kind}): score {score:.3f}",
                {"output": p.output, "target_kind": p.target_kind},
            )
        )
    kinds = {s[3] for s in scored}
    kind = (
        ComponentKind.CALIBRATED_PROBABILITY
        if kinds == {"CALIBRATED_PROBABILITY"}
        else ComponentKind.REGRESSION_ESTIMATE
        if kinds == {"REGRESSION_ESTIMATE"}
        else ComponentKind.RAW_MODEL_SCORE
    )
    return _available(
        "ml_support",
        statistics.fmean(values),
        statistics.fmean(s[2] for s in scored),
        f"{len(scored)} approved model prediction(s); uncalibrated outputs carry reduced "
        f"authority ({policy.uncalibrated_authority})",
        kind,
        models=[f"{s[0].model_name}@v{s[0].model_version}" for s in scored],
        disagreement=c.ml_disagreement,
    )


def _regime(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    timeframe = c.lead.timeframe
    market = ctx.market_regime(timeframe, "TREND")
    asset = ctx.asset_regime(c.asset_id, timeframe, "TREND")
    views: list[float] = []
    used: dict[str, Any] = {}
    opposed_families: list[str] = []
    for name, regime in (("market", market), ("asset", asset)):
        if regime is None:
            continue
        relation = _relation(regime.label, c.direction)
        if relation is None:
            continue
        family_scores = []
        for p in c.by_strategy.values():
            table = policy.regime_suitability[p.family]
            value = float(getattr(table, relation))
            family_scores.append(value)
            if relation == "opposed" and value <= 0.3:
                opposed_families.append(f"{p.family} vs {name} {regime.label}")
        views.append(statistics.fmean(family_scores))
        used[name] = {"label": regime.label, "relation": relation, "id": regime.id}
    if not views:
        return _missing(
            "regime_suitability",
            "no market or asset trend regime (other than UNCERTAIN) was available at the "
            "decision time",
        )
    score = statistics.fmean(views)
    vol = ctx.market_regime(timeframe, "VOLATILITY")
    if vol is not None and vol.label == "EXTREME":
        score *= policy.extreme_volatility_factor
        used["market_volatility"] = "EXTREME"
    if opposed_families:
        c.context["regime_opposed"] = opposed_families
    return _available(
        "regime_suitability",
        score,
        score,
        "policy suitability of the strategy families under the trend regime(s)",
        ComponentKind.HEURISTIC,
        **used,
    )


def _trend(c: Candidate, ctx: _Context) -> Component:
    f = ctx.asset_features(c.asset_id, c.lead.timeframe)
    if f is None or not f.features:
        return _missing("trend_alignment", "no Phase 3B asset features at the decision time")
    sign = _sign(c.direction)
    parts: dict[str, float] = {}
    close_vs_slow = f.features.get("close_vs_sma_slow")
    if close_vs_slow is not None:
        parts["close_vs_sma_slow"] = 1.0 if sign * close_vs_slow > 0 else 0.0
    slope_t = f.features.get("slow_slope_t")
    if slope_t is not None:
        parts["slow_slope_t"] = 0.5 if abs(slope_t) < 1 else (1.0 if sign * slope_t > 0 else 0.0)
    rel = f.features.get("relative_strength")
    if rel is not None:
        parts["relative_strength"] = 1.0 if sign * rel > 0 else 0.0
    if not parts:
        return _missing("trend_alignment", "the asset's trend features were undefined")
    return _available(
        "trend_alignment",
        statistics.fmean(parts.values()),
        None,
        "longer-term trend, slope significance and relative strength against the direction",
        ComponentKind.HEURISTIC,
        checks=parts,
        snapshot_id=f.snapshot_id,
    )


def _volatility(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    f = ctx.asset_features(c.asset_id, c.lead.timeframe)
    pct = None if f is None else f.features.get("volatility_percentile")
    regime = ctx.asset_regime(c.asset_id, c.lead.timeframe, "VOLATILITY")
    if pct is None and (regime is None or regime.label == "UNCERTAIN"):
        return _missing("volatility_conditions", "no asset volatility measurement available")
    score = 1.0
    if pct is not None and pct > policy.high_volatility_percentile:
        excess = (pct - policy.high_volatility_percentile) / (
            1 - policy.high_volatility_percentile or 1
        )
        score = 1.0 - 0.7 * clamp(excess)
    if regime is not None and regime.label == "EXTREME":
        score = min(score, 0.3)
    return _available(
        "volatility_conditions",
        score,
        pct,
        "penalizes unusually high asset volatility (percentile of its own history)",
        ComponentKind.HEURISTIC,
        volatility_percentile=pct,
        asset_volatility_regime=None if regime is None else regime.label,
    )


def _liquidity(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    f = ctx.asset_features(c.asset_id, c.lead.timeframe)
    mdv = None if f is None else f.median_dollar_volume
    if mdv is None or mdv <= 0:
        return _missing("liquidity", "no median dollar volume measured by the decision time")
    span = math.log(policy.liquidity_full_usd / policy.liquidity_floor_usd)
    score = clamp(math.log(max(mdv, 1e-9) / policy.liquidity_floor_usd) / span)
    return _available(
        "liquidity",
        score,
        mdv,
        "median daily dollar volume on a log scale between the policy floor and full level",
        ComponentKind.HEURISTIC,
        liquidity_as_of=None
        if f is None or f.liquidity_as_of is None
        else f.liquidity_as_of.isoformat(),
    )


def _costs_and_payoff(c: Candidate, policy: DecisionPolicy) -> list[Component]:
    geo = c.geometry
    assert geo is not None  # noqa: S101  # set before the components are computed
    out: list[Component] = []
    if geo.gross_reward is not None and geo.gross_reward > 0:
        out.append(
            _available(
                "cost_efficiency",
                1 - geo.cost / geo.gross_reward,
                geo.cost,
                f"assumed round-trip cost {policy.assumed_round_trip_cost_bps:g} bps "
                "(ASSUMPTION) against the potential reward",
                ComponentKind.HEURISTIC,
                cost_basis="ASSUMPTION",
            )
        )
    else:
        out.append(_missing("cost_efficiency", "no target level to compare costs with"))
    if geo.reward_risk is not None:
        out.append(
            _available(
                "reward_risk",
                geo.reward_risk / policy.reward_risk_saturation,
                geo.reward_risk,
                "net reward over net adverse move to the strategy's own levels",
                ComponentKind.HEURISTIC,
            )
        )
    else:
        out.append(_missing("reward_risk", "the lead proposal lacks a target or invalidation"))
    return out


def _news(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Component:
    start = ctx.bundle.decision_at - timedelta(hours=policy.news_lookback_hours)
    key = str(c.asset_id)
    analyses: list[LLMAnalysisInput] = [
        a
        for a in ctx.bundle.llm_analyses
        if key in a.sentiment and (a.subject_time is None or a.subject_time >= start)
    ]
    events = ctx.news.get(c.asset_id)
    if events is not None:
        c.evidence.append(
            Evidence(
                InputKind.NEWS_EVENTS,
                events.ref,
                EvidenceStance.CONTEXT,
                f"{events.event_count} Phase 3C event(s) directly related to the asset in the "
                "look-back window (classification only; not directional)",
                {"categories": events.categories},
            )
        )
    if not analyses:
        return _missing(
            "news",
            "no validated LLM news analysis for this asset was available at the decision "
            "time; event counts are context only (deterministic fallback)",
            phase_3c_events=None if events is None else events.event_count,
        )
    sign = _sign(c.direction)
    adjusted = [sign * a.sentiment[key] for a in analyses]
    mean = statistics.fmean(adjusted)
    for a in analyses:
        value = sign * a.sentiment[key]
        c.evidence.append(
            Evidence(
                InputKind.LLM_ANALYSIS,
                str(a.id),
                EvidenceStance.SUPPORTS
                if value > 0
                else EvidenceStance.OPPOSES
                if value < 0
                else EvidenceStance.NEUTRAL,
                f"LLM analysis ({a.model}, {a.prompt_version}; unverified estimate): "
                f"sentiment {a.sentiment[key]:+d} for the asset",
                {"event_type": a.event_type, "catalysts": a.catalysts},
            )
        )
    if mean <= -policy.news_conflict_threshold:
        c.context["news_conflict"] = mean
    return _available(
        "news",
        0.5 + 0.5 * mean,
        mean,
        f"mean direction-adjusted sentiment of {len(analyses)} LLM news analyses "
        "(unverified estimates; low weight)",
        ComponentKind.HEURISTIC,
        analyses=[str(a.id) for a in analyses],
    )


def _reliability(c: Candidate) -> Component:
    parts = []
    for p in c.by_strategy.values():
        share = p.snapshot_valid_count / p.snapshot_feature_count if p.snapshot_feature_count else 0
        factor = 1.0 if p.snapshot_basis is KnowledgeBasis.RECORDED else 0.8
        parts.append(share * factor)
    score = statistics.fmean(parts)
    return _available(
        "data_reliability",
        score,
        score,
        "share of VALID features behind the proposals (x0.8 for RECONSTRUCTED inputs)",
        ComponentKind.HEURISTIC,
        bases=sorted({p.snapshot_basis.value for p in c.by_strategy.values()}),
    )


def _score(c: Candidate, policy: DecisionPolicy) -> None:
    weights = policy.weights.as_dict()
    total = sum(weights.values())
    for comp in c.components:
        comp.weight = weights[comp.name]
    available = [x for x in c.components if x.status is ComponentStatus.AVAILABLE]
    avail_w = sum(x.weight for x in available)
    c.coverage = avail_w / total
    if avail_w > 0:
        for x in available:
            assert x.score is not None  # noqa: S101  # AVAILABLE components carry a score
            x.contribution = x.weight * x.score / avail_w
        c.heuristic = sum(x.contribution or 0.0 for x in available)
    scores = [x.score for x in available if x.score is not None]
    dispersion = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    reconstructed = any(p.snapshot_basis is not KnowledgeBasis.RECORDED for p in c.proposals)
    index = clamp(
        0.5 * (1 - c.coverage)
        + 0.3 * clamp(2 * dispersion)
        + 0.2 * clamp(2 * c.ml_disagreement)
        + (0.1 if reconstructed else 0.0)
    )
    c.uncertainty = {
        "index": index,
        "kind": "HEURISTIC_UNCERTAINTY_INDEX (not a confidence interval)",
        "component_coverage": c.coverage,
        "missing_components": sorted(
            x.name for x in c.components if x.status is not ComponentStatus.AVAILABLE
        ),
        "component_dispersion": dispersion,
        "ml_disagreement": c.ml_disagreement,
        "reconstructed_inputs": reconstructed,
        "expected_value_range": None
        if c.expected_value is None or c.expected_value.range_low is None
        else [c.expected_value.range_low, c.expected_value.range_high],
    }
    if c.heuristic is not None:
        c.ranking = c.heuristic * (1 - policy.uncertainty_penalty * index)


# --- Rules -----------------------------------------------------------------------------


def _apply_rules(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> None:
    decision_at = ctx.bundle.decision_at
    reliability = c.component("data_reliability")
    if c.heuristic is None or c.coverage < policy.min_component_coverage:
        c.decide(
            DecisionOutcome.INSUFFICIENT_DATA,
            "INSUFFICIENT_EVIDENCE",
            f"only {c.coverage:.0%} of the weighted evidence was available "
            f"(minimum {policy.min_component_coverage:.0%})",
        )
    if reliability is not None and (reliability.score or 0.0) < policy.min_data_reliability:
        c.decide(
            DecisionOutcome.INSUFFICIENT_DATA,
            "UNRELIABLE_DATA",
            f"data reliability {reliability.score or 0:.2f} below "
            f"{policy.min_data_reliability:.2f}",
        )
    geo = c.geometry
    if geo is not None and geo.reward_risk is not None and geo.reward_risk < policy.min_reward_risk:
        c.decide(
            DecisionOutcome.REJECT,
            "POOR_REWARD_RISK",
            f"net reward/risk {geo.reward_risk:.2f} after assumed costs is below "
            f"{policy.min_reward_risk:.2f}",
        )
    ev = c.expected_value
    if (
        policy.reject_negative_expected_value
        and ev is not None
        and ev.status is EstimateStatus.SUPPORTED
        and ev.expected_value is not None
        and ev.expected_value <= 0
    ):
        c.decide(
            DecisionOutcome.REJECT,
            "NEGATIVE_EXPECTED_VALUE",
            f"supported expected value {ev.expected_value:+.4f} is not positive",
        )
    if policy.reject_on_opposed_regime and c.context.get("regime_opposed"):
        c.decide(
            DecisionOutcome.REJECT,
            "UNFAVORABLE_REGIME",
            "trend regime opposes the strategy: " + "; ".join(c.context["regime_opposed"]),
        )
    newest_cutoff = max(p.data_cutoff for p in c.proposals)
    max_age = policy.max_data_age_hours.get(c.lead.timeframe, 96.0)
    age_hours = (decision_at - newest_cutoff).total_seconds() / 3600
    if age_hours > max_age:
        c.decide(
            DecisionOutcome.DEFER,
            "STALE_DATA",
            f"newest data cutoff is {age_hours:.0f} h old (maximum {max_age:g} h)",
        )
    vol = ctx.market_regime(c.lead.timeframe, "VOLATILITY")
    if vol is not None and vol.label in policy.defer_market_volatility:
        c.decide(
            DecisionOutcome.DEFER,
            "UNFAVORABLE_MARKET_CONDITIONS",
            f"market volatility regime {vol.label} (policy defers in this regime)",
        )
    if c.uncertainty.get("index", 1.0) > policy.max_uncertainty:
        c.decide(
            DecisionOutcome.DEFER,
            "HIGH_UNCERTAINTY",
            f"uncertainty index {c.uncertainty['index']:.2f} above {policy.max_uncertainty:.2f}",
        )
    if policy.defer_on_ml_disagreement and c.ml_disagreement > 0:
        c.decide(
            DecisionOutcome.DEFER,
            "ML_DISAGREEMENT",
            f"approved models disagree (spread {c.ml_disagreement:.2f})",
        )


def _context_conflicts(c: Candidate) -> list[ConflictDraft]:
    out: list[ConflictDraft] = []
    ids = sorted({p.id for p in c.proposals}, key=str)
    if c.ml_disagreement > 0:
        out.append(
            ConflictDraft(
                ConflictType.ML_DISAGREEMENT,
                c.asset_id,
                [c.key],
                ids,
                "recorded; uncertainty raised"
                + ("; deferred by policy" if c.outcome is DecisionOutcome.DEFER else ""),
                f"approved models disagree on {c.symbol} {c.direction.value} "
                f"(spread {c.ml_disagreement:.2f})",
                {"models": [str(p.model_version_id) for p in c.usable_ml]},
            )
        )
    if c.context.get("regime_opposed"):
        out.append(
            ConflictDraft(
                ConflictType.REGIME_CONFLICT,
                c.asset_id,
                [c.key],
                ids,
                "lowers regime suitability"
                + ("; rejected by policy" if c.outcome is DecisionOutcome.REJECT else ""),
                "market intelligence opposes the strategy: "
                + "; ".join(c.context["regime_opposed"]),
            )
        )
    if "news_conflict" in c.context:
        out.append(
            ConflictDraft(
                ConflictType.NEWS_PRICE_CONFLICT,
                c.asset_id,
                [c.key],
                ids,
                "recorded; lowers the news component (no rule overrides price evidence)",
                f"LLM news analyses lean against the price-based {c.direction.value} signal "
                f"(mean {c.context['news_conflict']:+.2f})",
            )
        )
    return out


def _resolve_opposing(candidates: list[Candidate], policy: DecisionPolicy) -> list[ConflictDraft]:
    by_key = {c.key: c for c in candidates}
    out: list[ConflictDraft] = []
    for c in candidates:
        if c.direction is not Direction.LONG:
            continue
        other = by_key.get((c.asset_id, Direction.SHORT))
        if other is None:
            continue
        pair = [c, other]
        ids = sorted({p.id for x in pair for p in x.proposals}, key=str)
        scored = [x for x in pair if x.outcome is None and x.ranking is not None]
        for x in pair:
            if x.ranking is not None:
                x.ranking = max(0.0, x.ranking - policy.conflict_penalty)
        if len(scored) == 2:
            gap = abs((c.ranking or 0) - (other.ranking or 0))
            if gap < policy.opposing_margin:
                for x in pair:
                    x.decide(
                        DecisionOutcome.DEFER,
                        "OPPOSING_PROPOSALS_UNRESOLVED",
                        f"LONG and SHORT opportunities on {c.symbol} are within "
                        f"{policy.opposing_margin:.2f} of each other ({gap:.2f})",
                    )
                handling = "both deferred: the evidence does not separate them"
            else:
                weaker = min(pair, key=lambda x: x.ranking or 0.0)
                weaker.decide(
                    DecisionOutcome.DEPRIORITIZE,
                    "OPPOSED_BY_STRONGER_OPPORTUNITY",
                    f"the opposite direction on {c.symbol} ranks higher by {gap:.2f}",
                )
                handling = (
                    f"{weaker.direction.value} deprioritized; the other side keeps a conflict "
                    f"penalty of {policy.conflict_penalty:.2f}"
                )
        else:
            handling = (
                f"conflict penalty {policy.conflict_penalty:.2f} applied; at least one side was "
                "already declined by a rule"
            )
        draft = ConflictDraft(
            ConflictType.OPPOSING_DIRECTIONS,
            c.asset_id,
            [c.key, other.key],
            ids,
            handling,
            f"strategies disagree on {c.symbol}: LONG ({', '.join(c.by_strategy)}) vs SHORT "
            f"({', '.join(other.by_strategy)})",
            {
                "long_ranking": c.ranking,
                "short_ranking": other.ranking,
            },
        )
        for x in pair:
            x.conflicts.append(draft)
        out.append(draft)
    return out


def _select(
    candidates: list[Candidate], ctx: _Context, policy: DecisionPolicy
) -> list[ConflictDraft]:
    out: list[ConflictDraft] = []
    pending = sorted(
        (c for c in candidates if c.outcome is None and c.ranking is not None),
        key=lambda c: (-(c.ranking or 0.0), str(c.asset_id), c.direction.value),
    )
    chosen: list[Candidate] = []
    for c in pending:
        if (c.ranking or 0.0) < policy.prioritize_threshold:
            c.decide(
                DecisionOutcome.DEPRIORITIZE,
                "BELOW_THRESHOLD",
                f"ranking score {c.ranking:.3f} below {policy.prioritize_threshold:.2f}",
            )
            continue
        rival = None
        for k in chosen:
            corr = ctx.correlations.get(frozenset((c.asset_id, k.asset_id)))
            if corr is None or abs(corr) < policy.correlation_threshold:
                continue
            same_exposure = (corr > 0) == (c.direction is k.direction)
            if same_exposure:
                rival = (k, corr)
                break
        if rival is not None:
            k, corr = rival
            c.decide(
                DecisionOutcome.DEPRIORITIZE,
                "CORRELATED_WITH_HIGHER_RANKED",
                f"correlation {corr:+.2f} with the higher-ranked {k.symbol} "
                f"{k.direction.value}: the same exposure competes for review capacity",
            )
            draft = ConflictDraft(
                ConflictType.CORRELATED_CAPACITY,
                None,
                [k.key, c.key],
                sorted({p.id for x in (k, c) for p in x.proposals}, key=str),
                f"{c.symbol} {c.direction.value} deprioritized in favour of {k.symbol}",
                f"{k.symbol} and {c.symbol} correlation {corr:+.2f}",
                {
                    "correlation": corr,
                    "relationship_id": ctx.correlation_refs.get(
                        frozenset((c.asset_id, k.asset_id))
                    ),
                },
            )
            c.conflicts.append(draft)
            k.conflicts.append(draft)
            out.append(draft)
            continue
        if len(chosen) >= policy.max_prioritized:
            c.decide(
                DecisionOutcome.DEPRIORITIZE,
                "REVIEW_CAPACITY",
                f"{policy.max_prioritized} higher-ranked opportunities were already prioritized",
            )
            continue
        c.decide(
            DecisionOutcome.PRIORITIZE_FOR_REVIEW,
            "PRIORITIZED",
            f"ranking score {c.ranking:.3f} >= {policy.prioritize_threshold:.2f}; a candidate "
            "for future risk review, not an approved trade",
        )
        chosen.append(c)
    ranked = sorted(
        (
            c
            for c in candidates
            if c.outcome in (DecisionOutcome.PRIORITIZE_FOR_REVIEW, DecisionOutcome.DEPRIORITIZE)
            and c.ranking is not None
        ),
        key=lambda c: (-(c.ranking or 0.0), str(c.asset_id), c.direction.value),
    )
    for i, c in enumerate(ranked, start=1):
        c.rank = i
    return out


# --- Records ---------------------------------------------------------------------------


def _explain(c: Candidate) -> str:
    assert c.outcome is not None  # noqa: S101  # every candidate is decided before this
    top = sorted(
        (x for x in c.components if x.contribution is not None),
        key=lambda x: -(x.contribution or 0.0),
    )[:3]
    lines = [
        f"{c.outcome.value}: {c.symbol} {c.direction.value} ({', '.join(c.timeframes)}), "
        f"proposed by {', '.join(c.by_strategy)}.",
    ]
    if c.heuristic is not None:
        lines.append(
            f"Heuristic score {c.heuristic:.3f}, ranking score {c.ranking or 0:.3f} "
            f"(coverage {c.coverage:.0%}; heuristics, not probabilities)."
        )
    if top:
        lines.append(
            "Largest contributions: "
            + ", ".join(f"{x.name} {x.score:.2f}x{x.weight:.2f}" for x in top)
            + "."
        )
    missing = c.uncertainty.get("missing_components") or []
    if missing:
        lines.append("Unavailable: " + ", ".join(missing) + ".")
    lines.extend(f"{r.code}: {r.message}." for r in c.reasons)
    if c.expected_value is not None and c.expected_value.status is EstimateStatus.NOT_SUPPORTED:
        lines.append("No expected value is claimed (no validated probability).")
    if c.outcome is DecisionOutcome.PRIORITIZE_FOR_REVIEW:
        lines.append("Analytical prioritization only: the risk engine has not reviewed it.")
    return " ".join(lines)


def _record(c: Candidate, ctx: _Context, policy: DecisionPolicy) -> Decision:
    assert c.outcome is not None  # noqa: S101
    bundle = ctx.bundle
    timeframe = c.lead.timeframe
    regime_snapshot: dict[str, Any] = {"market": {}, "asset": {}}
    for dim in ("TREND", "VOLATILITY", "STABILITY"):
        m = ctx.market_regime(timeframe, dim)
        if m is not None:
            regime_snapshot["market"][dim] = {
                "label": m.label,
                "confidence": m.confidence,
                "quality": m.quality,
                "basis": m.basis.value,
                "as_of": m.as_of.isoformat(),
                "available_at": m.available_at.isoformat(),
                "id": m.id,
            }
        a = ctx.asset_regime(c.asset_id, timeframe, dim)
        if a is not None:
            regime_snapshot["asset"][dim] = {
                "label": a.label,
                "confidence": a.confidence,
                "quality": a.quality,
                "as_of": a.as_of.isoformat(),
                "id": a.id,
            }
    features = ctx.asset_features(c.asset_id, timeframe)
    ts: dict[str, Any] = {
        "proposals": {
            str(p.id): {
                "signal_at": p.signal_at.isoformat(),
                "data_cutoff": p.data_cutoff.isoformat(),
                "stored_at": p.available_at.isoformat(),
                "inputs_available_at": None
                if p.inputs_available_at is None
                else p.inputs_available_at.isoformat(),
            }
            for p in c.proposals
        },
        "asset_features_as_of": None
        if features is None or features.as_of is None
        else features.as_of.isoformat(),
        "liquidity_as_of": None
        if features is None or features.liquidity_as_of is None
        else features.liquidity_as_of.isoformat(),
        "ml_predictions": {str(p.id): p.predicted_at.isoformat() for p in c.usable_ml},
    }
    ev = c.expected_value
    geo = c.geometry
    calibrated = next(
        (
            p
            for p in c.usable_ml
            if p.output_kind == "CALIBRATED_PROBABILITY"
            and number(p.output.get("probability")) is not None
        ),
        None,
    )
    ml = c.component("ml_support")
    scores = {
        "heuristic_score": c.heuristic,
        "ranking_score": c.ranking,
        "component_coverage": c.coverage,
        "raw_model_score": None if ml is None else ml.raw_value,
        "calibrated_probability": None if calibrated is None else calibrated.output["probability"],
        "calibrated_probability_meaning": None
        if calibrated is None
        else f"P({calibrated.target_kind} target) from approved {calibrated.model_name}",
        "signal_strengths": {k: float(p.signal_strength) for k, p in c.by_strategy.items()},
        "note": SCORE_NOTE,
    }
    expected = {
        **({} if geo is None else geo.to_json()),
        "cost_basis": f"ASSUMPTION: {policy.assumed_round_trip_cost_bps:g} bps round trip; "
        "short borrow fees not modelled",
        "lead_proposal_id": str(c.lead.id),
        "expected_value": None if ev is None else ev.to_json(),
        "direction_vs_profitability": "potential reward/adverse are distances to the "
        "strategy's levels; a probability of direction is not an expected profit",
    }
    llm_refs = [
        {
            "analysis_id": str(a.id),
            "provider": a.provider,
            "model": a.model,
            "prompt_version": a.prompt_version,
            "available_at": a.available_at.isoformat(),
        }
        for a in bundle.llm_analyses
        if str(c.asset_id) in a.sentiment
    ]
    news = ctx.news.get(c.asset_id)
    news_refs = (
        []
        if news is None
        else [
            {
                "asset_id": str(news.asset_id),
                "event_count": news.event_count,
                "event_ids": news.event_ids[:50],
                "window_start": news.window_start.isoformat(),
                "window_end": news.window_end.isoformat(),
            }
        ]
    )
    proposal_evidence = [
        Evidence(
            InputKind.STRATEGY_PROPOSAL,
            str(p.id),
            EvidenceStance.SUPPORTS
            if c.by_strategy[p.strategy_id].id == p.id
            else EvidenceStance.NEUTRAL,
            f"{p.strategy_id} v{p.strategy_version} {p.direction.value} {p.timeframe}: "
            f"strength {float(p.signal_strength):.3f} (uncalibrated)"
            + ("" if c.by_strategy[p.strategy_id].id == p.id else "; duplicate, not counted"),
            {"reference_entry": str(p.reference_entry)},
        )
        for p in c.proposals
    ]
    c.evidence[:0] = proposal_evidence
    model_versions = [
        {
            "model_version_id": str(p.model_version_id),
            "model_name": p.model_name,
            "version": p.model_version,
            "status_at_decision": p.status_at_decision,
            "output_kind": p.output_kind,
            "prediction_id": str(p.id),
            "used": p in c.usable_ml,
        }
        for p in bundle.ml_predictions
        if p.asset_id == c.asset_id and p.direction is c.direction
    ]
    return Decision(
        outcome=c.outcome,
        rank=c.rank,
        asset_id=c.asset_id,
        symbol=c.symbol,
        direction=c.direction,
        timeframes=c.timeframes,
        proposal_ids=sorted({p.id for p in c.proposals}, key=str),
        strategy_versions=[
            {
                "strategy_id": p.strategy_id,
                "strategy_version_id": str(p.strategy_version_id),
                "version": p.strategy_version,
                "logic_version": p.logic_version,
                "configuration_sha256": p.configuration_sha256,
            }
            for p in c.by_strategy.values()
        ],
        model_versions=model_versions,
        regime_snapshot=regime_snapshot,
        news_refs=news_refs,
        llm_refs=llm_refs,
        feature_timestamps=ts,
        scores=scores,
        expected=expected,
        uncertainty=c.uncertainty,
        conflicts=[x.to_json() for x in c.conflicts],
        reasons=[r.to_json() for r in c.reasons],
        explanation=_explain(c),
        decision_at=bundle.decision_at,
        data_cutoff=max(p.data_cutoff for p in c.proposals),
        expires_at=min(
            min(p.expires_at for p in c.proposals),
            bundle.decision_at + timedelta(hours=policy.decision_ttl_hours),
        ),
        components=[x.to_json() for x in c.components],
        evidence=[e.to_json() for e in c.evidence],
    )


def _no_opportunity(
    bundle: InputBundle, policy: DecisionPolicy, candidates: list[Candidate]
) -> Decision:
    counts: dict[str, int] = defaultdict(int)
    for c in candidates:
        counts[c.outcome.value if c.outcome else "UNDECIDED"] += 1
    if not bundle.proposals:
        reason = Reason("NO_PROPOSALS", "no active strategy proposal existed at the decision time")
    else:
        reason = Reason(
            "NONE_PRIORITIZED",
            f"{len(candidates)} opportunity(ies) evaluated; none met the prioritization rules",
        )
    cutoff = max((p.data_cutoff for p in bundle.proposals), default=bundle.decision_at)
    return Decision(
        outcome=DecisionOutcome.NO_OPPORTUNITY,
        rank=None,
        asset_id=None,
        symbol=None,
        direction=None,
        timeframes=[],
        proposal_ids=sorted({p.id for p in bundle.proposals}, key=str),
        strategy_versions=[],
        model_versions=[],
        regime_snapshot={},
        news_refs=[],
        llm_refs=[],
        feature_timestamps={},
        scores={"note": SCORE_NOTE},
        expected={},
        uncertainty={},
        conflicts=[],
        reasons=[
            reason.to_json(),
            Reason(
                "OUTCOME_COUNTS",
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none",
            ).to_json(),
        ],
        explanation=f"NO_OPPORTUNITY: {reason.message}. Declining every proposal is a valid "
        "outcome.",
        decision_at=bundle.decision_at,
        data_cutoff=min(cutoff, bundle.decision_at),
        expires_at=bundle.decision_at + timedelta(hours=policy.decision_ttl_hours),
        components=[],
        evidence=[],
    )


def evaluate(bundle: InputBundle, policy: DecisionPolicy) -> Evaluation:
    ctx = _Context(bundle)
    candidates, conflicts = group(bundle, policy)
    strategies_by_key: dict[tuple[uuid.UUID, Direction], set[str]] = defaultdict(set)
    for p in bundle.proposals:
        strategies_by_key[(p.asset_id, p.direction)].add(p.strategy_id)
    for c in candidates:
        c.geometry = geometry(c.lead, policy.assumed_round_trip_cost_bps)
        opposing = strategies_by_key.get((c.asset_id, _opposite(c.direction)), set())
        c.components = [
            _signal_quality(c),
            _agreement(c, opposing, policy),
            _track_record(c, ctx, policy),
            _ml_support(c, ctx, policy),
            _regime(c, ctx, policy),
            _trend(c, ctx),
            _volatility(c, ctx, policy),
            _liquidity(c, ctx, policy),
            *_costs_and_payoff(c, policy),
            _news(c, ctx, policy),
            _reliability(c),
        ]
        c.expected_value = estimate(c.lead, c.geometry, c.usable_ml)
        _score(c, policy)
        _apply_rules(c, ctx, policy)
    conflicts.extend(_resolve_opposing(candidates, policy))
    conflicts.extend(_select(candidates, ctx, policy))
    for c in candidates:
        extra = _context_conflicts(c)
        c.conflicts.extend(extra)
        conflicts.extend(extra)
    decisions = [_record(c, ctx, policy) for c in candidates]
    prioritized = sum(1 for d in decisions if d.outcome is DecisionOutcome.PRIORITIZE_FOR_REVIEW)
    if prioritized == 0:
        decisions.append(_no_opportunity(bundle, policy, candidates))
    outcome = RunOutcome.OPPORTUNITIES_PRIORITIZED if prioritized else RunOutcome.NO_OPPORTUNITY
    counts: dict[str, int] = defaultdict(int)
    for d in decisions:
        counts[d.outcome.value] += 1
    summary = {
        "outcomes": dict(sorted(counts.items())),
        "candidates": len(candidates),
        "prioritized": prioritized,
        "conflicts": len(conflicts),
        "inputs": {
            "proposals": len(bundle.proposals),
            "market_regimes": len(bundle.market_regimes),
            "asset_regimes": len(bundle.asset_regimes),
            "asset_features": len(bundle.asset_features),
            "correlations": len(bundle.correlations),
            "news_windows": len(bundle.news),
            "llm_analyses": len(bundle.llm_analyses),
            "ml_predictions": len(bundle.ml_predictions),
            "track_records": len(bundle.track_records),
        },
        "notes": bundle.notes,
        "logic_version": LOGIC_VERSION,
    }
    return Evaluation(decisions, conflicts, outcome, len(bundle.proposals), summary)


__all__ = [
    "SCORE_NOTE",
    "Candidate",
    "Component",
    "ConflictDraft",
    "Decision",
    "Evaluation",
    "evaluate",
    "group",
]

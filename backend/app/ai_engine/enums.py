"""Enumerations of the AI decision engine and the LLM layer (Phase 5B)."""

from __future__ import annotations

from enum import StrEnum


class DecisionOutcome(StrEnum):
    """What the coordinator concluded about one opportunity (or, NO_OPPORTUNITY, a run).

    None of these is an approval. PRIORITIZE_FOR_REVIEW makes an opportunity a candidate
    for the future deterministic risk engine (Phase 6), which alone can authorize anything.
    """

    PRIORITIZE_FOR_REVIEW = "PRIORITIZE_FOR_REVIEW"
    DEPRIORITIZE = "DEPRIORITIZE"
    REJECT = "REJECT"
    DEFER = "DEFER"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    # Run level: nothing was prioritized (no proposals, or every candidate was declined).
    NO_OPPORTUNITY = "NO_OPPORTUNITY"


class RunOutcome(StrEnum):
    OPPORTUNITIES_PRIORITIZED = "OPPORTUNITIES_PRIORITIZED"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"


class DecisionRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DecisionMode(StrEnum):
    LIVE = "LIVE"  # decision time = now
    HISTORICAL = "HISTORICAL"  # a past decision time, evaluated now


class InputKind(StrEnum):
    """Kinds of point-in-time inputs a run read (each stored with its exact payload)."""

    STRATEGY_PROPOSAL = "STRATEGY_PROPOSAL"
    PROPOSAL_CONFLICT = "PROPOSAL_CONFLICT"
    FEATURE_SNAPSHOT = "FEATURE_SNAPSHOT"
    MARKET_REGIME = "MARKET_REGIME"
    ASSET_REGIME = "ASSET_REGIME"
    ASSET_FEATURES = "ASSET_FEATURES"
    CROSS_ASSET = "CROSS_ASSET"
    NEWS_EVENTS = "NEWS_EVENTS"
    LLM_ANALYSIS = "LLM_ANALYSIS"
    ML_PREDICTION = "ML_PREDICTION"
    STRATEGY_TRACK_RECORD = "STRATEGY_TRACK_RECORD"


class ComponentKind(StrEnum):
    """What a scoring component's value is. Only CALIBRATED_PROBABILITY is a probability."""

    HEURISTIC = "HEURISTIC"  # a documented [0, 1] heuristic, not a probability
    RAW_MODEL_SCORE = "RAW_MODEL_SCORE"  # an uncalibrated model output (an ordering)
    CALIBRATED_PROBABILITY = "CALIBRATED_PROBABILITY"  # an approved, calibrated model
    REGRESSION_ESTIMATE = "REGRESSION_ESTIMATE"


class ComponentStatus(StrEnum):
    AVAILABLE = "AVAILABLE"  # computed and weighted into the heuristic score
    UNAVAILABLE = "UNAVAILABLE"  # its inputs did not exist at the decision time
    EXCLUDED = "EXCLUDED"  # inputs existed but a documented rule excluded them


class EvidenceStance(StrEnum):
    SUPPORTS = "SUPPORTS"
    OPPOSES = "OPPOSES"
    NEUTRAL = "NEUTRAL"
    CONTEXT = "CONTEXT"


class ConflictType(StrEnum):
    OPPOSING_DIRECTIONS = "OPPOSING_DIRECTIONS"  # strategies disagree on direction
    DUPLICATE_PROPOSALS = "DUPLICATE_PROPOSALS"  # nearly identical trades
    ML_DISAGREEMENT = "ML_DISAGREEMENT"  # approved models disagree
    REGIME_CONFLICT = "REGIME_CONFLICT"  # market intelligence opposes a strategy
    NEWS_PRICE_CONFLICT = "NEWS_PRICE_CONFLICT"  # news analysis contradicts price action
    CORRELATED_CAPACITY = "CORRELATED_CAPACITY"  # correlated opportunities compete


class EstimateStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"  # the validated information needed does not exist


class LLMTask(StrEnum):
    # One structured analysis per article: classification, event interpretation, summary,
    # asset relevance and catalysts (one call per batch of articles).
    NEWS_ANALYSIS = "NEWS_ANALYSIS"
    # A structured research summary for one asset from its stored news analyses.
    ASSET_RESEARCH_SUMMARY = "ASSET_RESEARCH_SUMMARY"
    # An explanation of conflicts the coordinator recorded (after the decision).
    CONFLICT_EXPLANATION = "CONFLICT_EXPLANATION"


class LLMSubjectKind(StrEnum):
    NEWS_REVISION = "NEWS_REVISION"
    ASSET = "ASSET"
    DECISION_RUN = "DECISION_RUN"


class LLMRequestStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    INVALID_OUTPUT = "INVALID_OUTPUT"  # the response failed strict schema validation
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"  # retries exhausted while rate limited
    BUDGET_REFUSED = "BUDGET_REFUSED"  # a spending, token or request limit would be exceeded
    UNPRICED = "UNPRICED"  # no pricing configured: spending limits cannot be enforced
    INTERRUPTED = "INTERRUPTED"  # the process stopped while the request was running


TERMINAL_LLM_STATUSES = tuple(s for s in LLMRequestStatus if s is not LLMRequestStatus.RUNNING)


class ValidationResult(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class ProviderKind(StrEnum):
    NONE = "none"  # no LLM configured: analyses are UNAVAILABLE
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"
    FIXTURE = "fixture"  # TEST FIXTURE responses (tests only; never market analysis)


class EvaluationVerdict(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_INCREMENTAL_VALUE = "NO_INCREMENTAL_VALUE"
    INCREMENTAL_VALUE_OBSERVED = "INCREMENTAL_VALUE_OBSERVED"


__all__ = [
    "TERMINAL_LLM_STATUSES",
    "ComponentKind",
    "ComponentStatus",
    "ConflictType",
    "DecisionMode",
    "DecisionOutcome",
    "DecisionRunStatus",
    "EstimateStatus",
    "EvaluationVerdict",
    "EvidenceStance",
    "InputKind",
    "LLMRequestStatus",
    "LLMSubjectKind",
    "LLMTask",
    "ProviderKind",
    "RunOutcome",
    "ValidationResult",
]

"""Enumerations of the Strategy Laboratory (Phase 4A; signal states and conflicts 4B)."""

from __future__ import annotations

from enum import StrEnum


class StrategyFamily(StrEnum):
    MOMENTUM = "MOMENTUM"
    TREND_FOLLOWING = "TREND_FOLLOWING"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT = "BREAKOUT"
    VOLATILITY = "VOLATILITY"
    EVENT_DRIVEN = "EVENT_DRIVEN"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TriggerKind(StrEnum):
    BAR_CLOSE = "BAR_CLOSE"  # a period of the strategy's timeframe completed and settled
    NEWS_EVENT = "NEWS_EVENT"  # a news event about the asset became available
    MANUAL = "MANUAL"  # an operator or the CLI asked for it (historical evaluation)


class EvaluationMode(StrEnum):
    LIVE = "LIVE"  # decision time = now, inputs as recorded by then
    HISTORICAL = "HISTORICAL"  # a past decision time, re-evaluated now


class EvaluationStatus(StrEnum):
    RUNNING = "RUNNING"
    PROPOSED = "PROPOSED"  # completed with one or more proposals
    NO_PROPOSAL = "NO_PROPOSAL"  # completed; the strategy proposed nothing
    SKIPPED = "SKIPPED"  # a required feature was not VALID; the strategy was not called
    FAILED = "FAILED"  # see strategy_evaluation_errors


TERMINAL_STATUSES = (
    EvaluationStatus.PROPOSED,
    EvaluationStatus.NO_PROPOSAL,
    EvaluationStatus.SKIPPED,
    EvaluationStatus.FAILED,
)


class SignalState(StrEnum):
    """What an evaluation found (Phase 4B), finer than its status.

    PROPOSED evaluations are SIGNAL; NO_PROPOSAL ones are NO_SIGNAL or DUPLICATE; SKIPPED
    ones could not assess a signal (data or eligibility); FAILED ones are FAILED. Whether a
    recorded proposal later EXPIRED is a property of the proposal, not of the evaluation.
    """

    SIGNAL = "SIGNAL"  # one or more valid proposals were recorded
    NO_SIGNAL = "NO_SIGNAL"  # the inputs were valid; the conditions were not met
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # not enough history for a required feature
    MISSING_DATA = "MISSING_DATA"  # a period in a window has no usable bar (gap or stale)
    UNAVAILABLE_DATA = "UNAVAILABLE_DATA"  # a source did not cover the window or is stale
    INVALID_DATA = "INVALID_DATA"  # undefined values or inconsistent prices
    INELIGIBLE_ASSET = "INELIGIBLE_ASSET"  # not tradable / unsupported at the cutoff
    DUPLICATE = "DUPLICATE"  # the same proposal was already recorded for this event
    FAILED = "FAILED"  # see strategy_evaluation_errors


class ConflictKind(StrEnum):
    # Overlapping active proposals for the same asset in opposite directions. Recorded,
    # never resolved here: proposal competition belongs to the Phase 5 coordinator.
    OPPOSING_DIRECTION = "OPPOSING_DIRECTION"


class ErrorStage(StrEnum):
    INPUTS = "INPUTS"  # loading data or computing features
    STRATEGY = "STRATEGY"  # the strategy's own code raised
    VALIDATION = "VALIDATION"  # the strategy returned an invalid proposal
    TIMEOUT = "TIMEOUT"
    PERSISTENCE = "PERSISTENCE"
    INTERRUPTED = "INTERRUPTED"  # the process stopped while the evaluation was running


class ProposalEventKind(StrEnum):
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"  # a newer proposal of the same strategy, asset and direction


class ProposalStatus(StrEnum):
    """Derived at read time from expiry and lifecycle events. There is deliberately no
    "approved" state: a proposal is a hypothesis; approval belongs to the risk engine."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class StrengthKind(StrEnum):
    # A relative score in [0, 1]; NOT a probability. A calibrated kind needs separate
    # validation evidence (Phase 8) before it may be added.
    UNCALIBRATED_SCORE = "UNCALIBRATED_SCORE"


__all__ = [
    "TERMINAL_STATUSES",
    "ConflictKind",
    "Direction",
    "ErrorStage",
    "EvaluationMode",
    "EvaluationStatus",
    "ProposalEventKind",
    "ProposalStatus",
    "SignalState",
    "StrategyFamily",
    "StrengthKind",
    "TriggerKind",
]

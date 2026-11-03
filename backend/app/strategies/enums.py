"""Enumerations of the Strategy Laboratory (Phase 4A)."""

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
    "Direction",
    "ErrorStage",
    "EvaluationMode",
    "EvaluationStatus",
    "ProposalEventKind",
    "ProposalStatus",
    "StrategyFamily",
    "StrengthKind",
    "TriggerKind",
]

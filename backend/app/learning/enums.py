"""Enumerations of the validation laboratory and model governance (Phase 8B)."""

from __future__ import annotations

from enum import StrEnum


class ExperimentKind(StrEnum):
    # Parameter search inside chronological walk-forward folds, then out-of-sample tests.
    WALK_FORWARD = "WALK_FORWARD"
    # Fixed configurations (one or several strategies together) evaluated on the same
    # chronological folds without any search: independent and combined evaluation.
    EVALUATION = "EVALUATION"


class SearchMethod(StrEnum):
    NONE = "NONE"  # the stored parameters only (EVALUATION experiments)
    GRID = "GRID"  # every combination; refused above the trial budget
    RANDOM = "RANDOM"  # seeded uniform draws from the space
    BAYESIAN = "BAYESIAN"  # seeded Gaussian-process expected improvement after random starts


class WindowMode(StrEnum):
    ROLLING = "ROLLING"  # a training window of fixed length moves forward
    EXPANDING = "EXPANDING"  # training always starts at the development start


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


FINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


class TrialSource(StrEnum):
    STORED = "STORED"  # the strategy's stored (champion) parameters: always trial 0
    GRID = "GRID"
    RANDOM = "RANDOM"
    BAYESIAN = "BAYESIAN"
    NEIGHBOR = "NEIGHBOR"  # added by the sensitivity analysis, never selectable


class TrialStatus(StrEnum):
    OK = "OK"
    FAILED = "FAILED"  # the replay raised; recorded and counted as a trial


class Sample(StrEnum):
    IN_SAMPLE = "IN_SAMPLE"  # the training window the parameters were selected on
    VALIDATION = "VALIDATION"  # the window that chose among the best training candidates
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"  # walk-forward test windows (stitched)
    HOLDOUT = "HOLDOUT"  # the untouched final period, evaluated once on request


class SegmentKind(StrEnum):
    OVERALL = "OVERALL"
    STRATEGY = "STRATEGY"
    ASSET = "ASSET"
    ASSET_CLASS = "ASSET_CLASS"
    TIMEFRAME = "TIMEFRAME"
    DIRECTION = "DIRECTION"
    MARKET_TREND = "MARKET_TREND"
    VOLATILITY = "VOLATILITY"
    PERIOD = "PERIOD"


class RobustnessMethod(StrEnum):
    PURGED_KFOLD = "PURGED_KFOLD"
    CPCV = "CPCV"
    PBO = "PBO"
    DEFLATED_SHARPE = "DEFLATED_SHARPE"
    MULTIPLE_TESTING = "MULTIPLE_TESTING"
    PARAMETER_SENSITIVITY = "PARAMETER_SENSITIVITY"
    MONTE_CARLO_TRADES = "MONTE_CARLO_TRADES"
    BLOCK_BOOTSTRAP = "BLOCK_BOOTSTRAP"
    COST_STRESS = "COST_STRESS"
    SLIPPAGE_STRESS = "SLIPPAGE_STRESS"
    REGIME_SEGMENTATION = "REGIME_SEGMENTATION"
    OOS_DEGRADATION = "OOS_DEGRADATION"


class ResultStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"  # computed but below the stated minimum
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_RUN = "NOT_RUN"


class Finding(StrEnum):
    """What the evidence says about a strategy (several can apply)."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONSISTENT = "CONSISTENT"
    REGIME_DEPENDENT = "REGIME_DEPENDENT"
    DEGRADES_OUT_OF_SAMPLE = "DEGRADES_OUT_OF_SAMPLE"
    DEPENDS_ON_FEW_TRADES = "DEPENDS_ON_FEW_TRADES"
    UNPROFITABLE_AFTER_COSTS = "UNPROFITABLE_AFTER_COSTS"
    UNPROFITABLE = "UNPROFITABLE"
    EXCESSIVE_DRAWDOWN = "EXCESSIVE_DRAWDOWN"
    LIKELY_OVERFIT = "LIKELY_OVERFIT"
    COST_SENSITIVE = "COST_SENSITIVE"
    PARAMETER_UNSTABLE = "PARAMETER_UNSTABLE"


class Verdict(StrEnum):
    """The run's overall conclusion. There is deliberately no "proven" verdict."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_ROBUST = "NOT_ROBUST"  # evidence exists and it is negative or fragile
    CANDIDATE_FOR_REVIEW = "CANDIDATE_FOR_REVIEW"  # passed every gate; a human decides


class SubjectKind(StrEnum):
    STRATEGY = "STRATEGY"
    MODEL = "MODEL"


class CandidateStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATING = "VALIDATING"
    SHADOW = "SHADOW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RETIRE = "RETIRE"
    ROLLBACK = "ROLLBACK"


class ActorKind(StrEnum):
    USER = "USER"  # an operator (API token or CLI)
    SYSTEM = "SYSTEM"  # the monitor, acting only under an approved rule


class MonitorMetric(StrEnum):
    EXPECTANCY_SHIFT = "EXPECTANCY_SHIFT"
    WIN_RATE_SHIFT = "WIN_RATE_SHIFT"
    DRAWDOWN = "DRAWDOWN"
    COST_PER_TRADE = "COST_PER_TRADE"
    RETURN_DISTRIBUTION = "RETURN_DISTRIBUTION"
    FEATURE_PSI = "FEATURE_PSI"
    FEATURE_KS = "FEATURE_KS"
    CALIBRATION_ECE = "CALIBRATION_ECE"
    PREDICTION_SHIFT = "PREDICTION_SHIFT"
    REGIME_FAILURE = "REGIME_FAILURE"


class MeasurementStatus(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    BREACH = "BREACH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AlertAction(StrEnum):
    NONE = "NONE"
    WARNED = "WARNED"
    SUSPENDED = "SUSPENDED"  # a strategy disabled under an approved SUSPEND rule


class RuleAction(StrEnum):
    WARN = "WARN"
    SUSPEND = "SUSPEND"  # strategies only: disable it (never enable, never change limits)

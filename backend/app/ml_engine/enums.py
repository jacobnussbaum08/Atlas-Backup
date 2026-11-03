"""Enumerations of the machine-learning engine (Phase 5A)."""

from __future__ import annotations

from enum import StrEnum


class DataOrigin(StrEnum):
    """Where a dataset's observations came from. TEST_FIXTURE data is never market data."""

    REAL_HISTORICAL = "REAL_HISTORICAL"  # bars stored by the market-data engine
    TEST_FIXTURE = "TEST_FIXTURE"  # synthetic series written by tests (labelled as such)


class ObservationSource(StrEnum):
    """What makes a decision time an observation."""

    # Every completed period of every asset (LONG and/or SHORT): generic market outcomes.
    PERIOD_CLOSE = "PERIOD_CLOSE"
    # The Phase 4B strategies evaluated in process at every completed period (nothing is
    # stored); an observation is a proposal the strategy would have made.
    STRATEGY_REPLAY = "STRATEGY_REPLAY"
    # Proposals the Strategy Laboratory actually recorded (strategy_proposals).
    STRATEGY_PROPOSALS = "STRATEGY_PROPOSALS"


class UniversePolicy(StrEnum):
    # The listed assets, whatever their universe status was at the time. Survivorship
    # bias is possible: the list is chosen today. Every such dataset carries a warning.
    FIXED_ASSETS = "FIXED_ASSETS"
    # Only decision times at which the asset was a member of the Phase 3A qualified
    # universe (universe_membership_events known by then).
    QUALIFIED_AT_DECISION = "QUALIFIED_AT_DECISION"


class EntryReference(StrEnum):
    NEXT_OPEN = "NEXT_OPEN"  # open of the first period after the decision (tradable)
    DECISION_CLOSE = "DECISION_CLOSE"  # close of the decision period (not tradable: research)


class TargetKind(StrEnum):
    FORWARD_RETURN = "FORWARD_RETURN"  # regression: net direction-adjusted return
    DIRECTION = "DIRECTION"  # classification: net return above a threshold
    VOL_ADJUSTED_RETURN = "VOL_ADJUSTED_RETURN"  # regression: net return / ex-ante vol
    VOL_ADJUSTED_DIRECTION = "VOL_ADJUSTED_DIRECTION"  # classification on the above
    BARRIER = "BARRIER"  # classification: profit barrier reached before the stop


class BarrierSource(StrEnum):
    ATR_MULTIPLES = "ATR_MULTIPLES"  # entry +/- multiples of ATR known at the decision
    STRATEGY_LEVELS = "STRATEGY_LEVELS"  # the proposal's invalidation and target levels


class TaskKind(StrEnum):
    CLASSIFICATION = "CLASSIFICATION"
    REGRESSION = "REGRESSION"


class LabelStatus(StrEnum):
    MATURED = "MATURED"  # the outcome period completed and settled by data_known_at
    IMMATURE = "IMMATURE"  # the horizon had not ended by data_known_at; never trained on
    UNDEFINED = "UNDEFINED"  # a bar on the path is missing or invalid; never trained on


class ExitReason(StrEnum):
    HORIZON = "HORIZON"
    PROFIT_BARRIER = "PROFIT_BARRIER"
    STOP_BARRIER = "STOP_BARRIER"


class ModelType(StrEnum):
    LOGISTIC_REGRESSION = "LOGISTIC_REGRESSION"  # ridge regression for regression targets
    RANDOM_FOREST = "RANDOM_FOREST"
    XGBOOST = "XGBOOST"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ValidationStatus(StrEnum):
    """Out-of-sample evidence recorded for a model version (not a promotion)."""

    PASSED = "PASSED"  # beat its baselines on the untouched test period and walk-forward
    FAILED = "FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # too few test observations to judge


class LifecycleStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class OutputKind(StrEnum):
    # Calibrated on validation data, with out-of-sample calibration evidence.
    CALIBRATED_PROBABILITY = "CALIBRATED_PROBABILITY"
    UNCALIBRATED_SCORE = "UNCALIBRATED_SCORE"  # an ordering score, not a probability
    REGRESSION_ESTIMATE = "REGRESSION_ESTIMATE"  # an estimate of the target value


class CalibrationMethod(StrEnum):
    NONE = "NONE"
    SIGMOID = "SIGMOID"  # Platt scaling
    ISOTONIC = "ISOTONIC"


class SplitKind(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    TEST = "TEST"  # the untouched final period
    WALK_FORWARD = "WALK_FORWARD"
    PURGED_KFOLD = "PURGED_KFOLD"
    CPCV = "CPCV"


class SegmentKind(StrEnum):
    ALL = "ALL"
    ASSET = "ASSET"
    TIMEFRAME = "TIMEFRAME"
    DIRECTION = "DIRECTION"
    STRATEGY = "STRATEGY"
    MARKET_TREND = "MARKET_TREND"
    VOLATILITY = "VOLATILITY"


class PredictionMode(StrEnum):
    APPROVED = "APPROVED"  # the approved (champion) model
    SHADOW = "SHADOW"  # a shadow model: recorded, never acted on


class PredictionValidity(StrEnum):
    VALID = "VALID"
    REJECTED_MODEL_STATUS = "REJECTED_MODEL_STATUS"
    REJECTED_SCHEMA = "REJECTED_SCHEMA"  # missing or incompatible features
    REJECTED_INVALID_VALUES = "REJECTED_INVALID_VALUES"  # non-finite or wrong-typed values
    REJECTED_ARTIFACT = "REJECTED_ARTIFACT"  # the artifact is missing or its hash differs


__all__ = [
    "BarrierSource",
    "CalibrationMethod",
    "DataOrigin",
    "EntryReference",
    "ExitReason",
    "LabelStatus",
    "LifecycleStatus",
    "ModelType",
    "ObservationSource",
    "OutputKind",
    "PredictionMode",
    "PredictionValidity",
    "RunStatus",
    "SegmentKind",
    "SplitKind",
    "TargetKind",
    "TaskKind",
    "UniversePolicy",
    "ValidationStatus",
]

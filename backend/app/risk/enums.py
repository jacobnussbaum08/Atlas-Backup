"""Enumerations of the deterministic risk engine (Phase 6B, spec 14)."""

from __future__ import annotations

from enum import StrEnum


class AssessmentResult(StrEnum):
    """The outcome of one risk assessment.

    PASS_FOR_RESERVATION is **not** an execution approval: it says the proposal satisfies every
    hard limit at assessment time and may be offered to the atomic reservation step of Phase
    6C, which re-checks everything under the account lock before anything is reserved.
    """

    PASS_FOR_RESERVATION = "PASS_FOR_RESERVATION"  # noqa: S105  # a result, not a credential
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    RISK_HALTED = "RISK_HALTED"


class TradeDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class ProposalSource(StrEnum):
    """Where the levels of an assessed proposal came from (provenance only)."""

    OPERATOR = "OPERATOR"
    STRATEGY_PROPOSAL = "STRATEGY_PROPOSAL"
    AI_DECISION = "AI_DECISION"


class CheckOutcome(StrEnum):
    PASS = "PASS"  # noqa: S105  # the check ran and the proposal satisfies it
    FAIL = "FAIL"  # the check ran and the proposal violates it (REJECTED)
    UNKNOWN = "UNKNOWN"  # the check could not run: required data is missing (INSUFFICIENT_DATA)
    HALT = "HALT"  # an active circuit breaker blocks new entries (RISK_HALTED)
    RESTRICT = "RESTRICT"  # the check reduced the permissible quantity
    INFO = "INFO"  # a recorded fact or assumption that decides nothing


class BreakerType(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    EXCESSIVE_EXPOSURE = "EXCESSIVE_EXPOSURE"
    DATA_QUALITY = "DATA_QUALITY"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    EXECUTION_SAFETY = "EXECUTION_SAFETY"
    KILL_SWITCH = "KILL_SWITCH"


class BreakerStatus(StrEnum):
    CLEAR = "CLEAR"
    TRIPPED = "TRIPPED"


class BreakerEvent(StrEnum):
    TRIPPED = "TRIPPED"
    RESET = "RESET"


class RecoveryMode(StrEnum):
    """How a tripped breaker returns to CLEAR. Fixed per breaker type (decision D-141)."""

    OPERATOR_RESET = "OPERATOR_RESET"  # an operator resets it, with a reason, once clear
    AUTO_WHEN_CLEAR = "AUTO_WHEN_CLEAR"  # the monitor records a RESET once the data is fresh


class ActorKind(StrEnum):
    OPERATOR = "OPERATOR"
    SYSTEM = "SYSTEM"


class ConfigChangeKind(StrEnum):
    INITIAL = "INITIAL"
    HARD_LIMITS = "HARD_LIMITS"
    PREFERENCES = "PREFERENCES"
    HARD_LIMITS_AND_PREFERENCES = "HARD_LIMITS_AND_PREFERENCES"


class PeriodKind(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class BaselineBasis(StrEnum):
    """How the equity at the start of a loss period was established."""

    # An equity observation at or before the boundary, within the preceding period.
    OBSERVED_AT_BOUNDARY = "OBSERVED_AT_BOUNDARY"
    # The account was funded during the period: the opening funding is the baseline.
    FUNDED_IN_PERIOD = "FUNDED_IN_PERIOD"
    # No usable observation before the boundary: the first one inside the period is used
    # and losses between the boundary and that observation are not captured (flagged).
    FIRST_OBSERVATION_IN_PERIOD = "FIRST_OBSERVATION_IN_PERIOD"


class MarkStatus(StrEnum):
    """Whether every open position and foreign currency could be marked to fresh inputs."""

    COMPLETE = "COMPLETE"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"


class SnapshotTrigger(StrEnum):
    ASSESSMENT = "ASSESSMENT"
    MONITOR = "MONITOR"
    BREAKER_RESET = "BREAKER_RESET"


class RiskEventType(StrEnum):
    CONFIGURATION_CREATED = "CONFIGURATION_CREATED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    ASSESSMENT_RECORDED = "ASSESSMENT_RECORDED"
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    BREAKER_RESET = "BREAKER_RESET"
    BREAKER_RESET_REFUSED = "BREAKER_RESET_REFUSED"
    STATE_EVALUATED = "STATE_EVALUATED"
    CLASSIFICATION_RECORDED = "CLASSIFICATION_RECORDED"
    # Phase 6C atomic reservations (the full history is ``risk_reservation_events``).
    RESERVATION_APPROVED = "RESERVATION_APPROVED"
    RESERVATION_REJECTED = "RESERVATION_REJECTED"
    RESERVATION_CONSUMED = "RESERVATION_CONSUMED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_FAILED = "RESERVATION_FAILED"
    RESERVATION_UNCERTAIN = "RESERVATION_UNCERTAIN"
    RESERVATION_RECONCILED = "RESERVATION_RECONCILED"
    RESERVATION_MISMATCH = "RESERVATION_MISMATCH"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ClassificationSource(StrEnum):
    OPERATOR = "OPERATOR"
    TEST_FIXTURE = "TEST_FIXTURE"


class MissingDataPolicy(StrEnum):
    REJECT = "REJECT"
    # Use the configured conservative minimum instead (recorded as an assumption).
    USE_CONFIGURED_MINIMUM = "USE_CONFIGURED_MINIMUM"


class MissingCorrelationPolicy(StrEnum):
    # A held asset without a usable correlation measurement counts as correlated.
    ASSUME_CORRELATED = "ASSUME_CORRELATED"
    # ... or as uncorrelated (it still counts toward every other limit).
    ASSUME_UNCORRELATED = "ASSUME_UNCORRELATED"


class UnclassifiedSectorPolicy(StrEnum):
    # Every asset without a sector shares one UNCLASSIFIED bucket under the sector limit.
    POOL = "POOL"
    REJECT = "REJECT"


class ReservationStatus(StrEnum):
    """Lifecycle of an atomic risk reservation (Phase 6C, decision D-148).

    PENDING exists only inside the transaction that creates the reservation: the same
    transaction decides it (APPROVED or REJECTED), and the database refuses to commit a
    PENDING reservation. APPROVED and PARTIALLY_CONSUMED hold capacity; the rest are final.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


ACTIVE_RESERVATION_STATUSES = (ReservationStatus.APPROVED, ReservationStatus.PARTIALLY_CONSUMED)
FINAL_RESERVATION_STATUSES = (
    ReservationStatus.CONSUMED,
    ReservationStatus.RELEASED,
    ReservationStatus.EXPIRED,
    ReservationStatus.REJECTED,
    ReservationStatus.FAILED,
)


class ReservationEventType(StrEnum):
    CREATED = "CREATED"  # -> PENDING
    APPROVED = "APPROVED"  # PENDING -> APPROVED (capacity reserved)
    REJECTED = "REJECTED"  # PENDING -> REJECTED (nothing reserved)
    ORDER_ATTACHED = "ORDER_ATTACHED"  # an execution reference now depends on it
    CONSUMED = "CONSUMED"  # a fill converted part or all of it into a ledger position
    RELEASED = "RELEASED"  # the unconsumed remainder is given back (order resolved)
    EXPIRED = "EXPIRED"  # nothing was attached or consumed before it expired
    FAILED = "FAILED"  # a confirmed downstream failure; the remainder is given back
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"  # held for review: nothing is released
    EXPIRY_DEFERRED = "EXPIRY_DEFERRED"  # past expiry but an order depends on it: held


class ExecutionLink(StrEnum):
    """What depends on a reservation downstream (decides whether it may expire)."""

    NONE = "NONE"  # no order was attached: it can expire safely
    ORDER_ATTACHED = "ORDER_ATTACHED"  # an order may be live: never released automatically
    UNCERTAIN = "UNCERTAIN"  # the execution state is unknown: held until resolved


class ReleaseResolution(StrEnum):
    """Why an unconsumed remainder is released. Each needs a confirmed downstream state."""

    NOT_SUBMITTED = "NOT_SUBMITTED"  # no order was ever attached
    ORDER_CANCELLED = "ORDER_CANCELLED"  # cancellation confirmed (spec 15.1)
    ORDER_REJECTED = "ORDER_REJECTED"  # the order was rejected downstream
    ORDER_EXPIRED = "ORDER_EXPIRED"  # the order expired unfilled (or partly filled)
    ORDER_COMPLETED = "ORDER_COMPLETED"  # the order is done; its remainder will never fill
    OPERATOR = "OPERATOR"  # an operator released it explicitly, with a reason


class ReservationCommand(StrEnum):
    RESERVE = "RESERVE"
    ATTACH_ORDER = "ATTACH_ORDER"
    CONSUME = "CONSUME"
    RELEASE = "RELEASE"
    FAIL = "FAIL"
    FLAG_UNCERTAIN = "FLAG_UNCERTAIN"


class ReconciliationOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


__all__ = [
    "ACTIVE_RESERVATION_STATUSES",
    "FINAL_RESERVATION_STATUSES",
    "ActorKind",
    "AssessmentResult",
    "BaselineBasis",
    "BreakerEvent",
    "BreakerStatus",
    "BreakerType",
    "CheckOutcome",
    "ClassificationSource",
    "ConfigChangeKind",
    "ExecutionLink",
    "MarkStatus",
    "MissingCorrelationPolicy",
    "MissingDataPolicy",
    "PeriodKind",
    "ProposalSource",
    "ReconciliationOutcome",
    "RecoveryMode",
    "ReleaseResolution",
    "ReservationCommand",
    "ReservationEventType",
    "ReservationStatus",
    "RiskEventType",
    "Severity",
    "SnapshotTrigger",
    "TradeDirection",
    "UnclassifiedSectorPolicy",
]

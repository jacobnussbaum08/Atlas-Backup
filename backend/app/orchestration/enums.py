"""Orchestration enums (Phase 10A)."""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    """Whether the orchestrator may open new paper positions for an account.

    Only ENABLED lets new entries through. Every other mode still lets exits, protective
    orders, monitoring, reconciliation and risk checks run (spec 24.2: halting entries must
    never prevent reducing risk).
    """

    DISABLED = "DISABLED"  # the default: no record means DISABLED
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"  # temporarily no new entries; resume returns to ENABLED
    EMERGENCY_STOP = "EMERGENCY_STOP"  # kill switch engaged, working entries cancelled


class ControlAction(StrEnum):
    ENABLE = "ENABLE"
    DISABLE = "DISABLE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    CLEAR_EMERGENCY = "CLEAR_EMERGENCY"  # back to DISABLED, never straight to ENABLED


class IntentStatus(StrEnum):
    """One prioritized AI decision's way through the risk engine and execution engine."""

    RECEIVED = "RECEIVED"  # taken in; its outbox event is pending
    ORDER_CREATED = "ORDER_CREATED"  # reserved and a simulated paper order was created
    RISK_REJECTED = "RISK_REJECTED"  # the risk engine refused it: a no-trade decision
    EXECUTION_REFUSED = "EXECUTION_REFUSED"  # reserved, but execution validation refused it
    NOT_TRADED = "NOT_TRADED"  # trading was not enabled when it was handled
    INVALID = "INVALID"  # the decision could not be offered to the risk engine
    DEAD = "DEAD"  # failed every attempt; nothing was reserved or ordered


TERMINAL_INTENT = frozenset(IntentStatus) - {IntentStatus.RECEIVED}


class JobKind(StrEnum):
    RISK_MONITOR = "RISK_MONITOR"
    RESERVATION_RECONCILE = "RESERVATION_RECONCILE"
    EXECUTION_RECONCILE = "EXECUTION_RECONCILE"
    ACCOUNTING_RECONCILE = "ACCOUNTING_RECONCILE"
    WORKER_HEALTH = "WORKER_HEALTH"
    MARKET_INTELLIGENCE = "MARKET_INTELLIGENCE"
    DECISION_OUTCOMES = "DECISION_OUTCOMES"


class JobStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"  # will be retried at next_attempt_at
    DEAD = "DEAD"  # gave up after the maximum attempts


class OrchestratorState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"  # the last tick failed (e.g. the database was unreachable)
    STOPPED = "STOPPED"

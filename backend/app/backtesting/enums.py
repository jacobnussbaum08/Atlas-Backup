"""Enumerations of the backtesting engine (Phase 8A)."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "QUEUED"  # stored with its resolved configuration, not started
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # an error stopped the replay; partial results are not stored


FINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


class UniverseMode(StrEnum):
    # A fixed list of symbols chosen by the user. Chosen today, so it may carry selection
    # or survivorship bias; the run says so.
    EXPLICIT = "EXPLICIT"
    # Members of a Phase 3A universe layer as Atlas had recorded them at each decision time.
    POINT_IN_TIME = "POINT_IN_TIME"


class FeatureAdjustment(StrEnum):
    # The provider's split-adjusted series, rescaled at each decision to the split factor
    # known then (no future split enters a past decision).
    POINT_IN_TIME_SPLIT = "POINT_IN_TIME_SPLIT"
    # Unadjusted bars: no corporate-action information at all; returns across a split are
    # discontinuous.
    RAW = "RAW"


class EntryOrderType(StrEnum):
    LIMIT = "LIMIT"  # at the approval's worst entry price (the reference entry on the tick)
    MARKET = "MARKET"  # bounded by the worst entry price (7B entry protection)
    STOP = "STOP"  # stop at the reference entry (breakout-style), bounded likewise
    STOP_LIMIT = "STOP_LIMIT"  # stop at the reference entry, limit at the worst entry price


class StopOrderType(StrEnum):
    STOP = "STOP"
    TRAILING_STOP = "TRAILING_STOP"


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    STOP = "STOP"  # protective stop (or trailing stop) of a trade
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"  # a market exit (holding horizon, corporate action)


class ProposalOutcome(StrEnum):
    """What became of one strategy proposal in the run."""

    REJECTED = "REJECTED"  # risk REJECTED / INSUFFICIENT_DATA / RISK_HALTED
    EXPIRED = "EXPIRED"  # approved, the entry order expired without a fill
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # entry expired or ended with part filled
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # approved, the order was cancelled (corporate action)
    OPEN_AT_END = "OPEN_AT_END"  # the entry order was still working when the run ended


class TradeStatus(StrEnum):
    CLOSED = "CLOSED"
    OPEN_AT_END = "OPEN_AT_END"


class RejectionStage(StrEnum):
    STRATEGY_VALIDATION = "STRATEGY_VALIDATION"  # a draft left out (eligibility, strength)
    RISK = "RISK"  # the 6B assessment did not pass
    EXECUTION = "EXECUTION"  # the entry order expired, was cancelled or could not fill


class RunEventKind(StrEnum):
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    BREAKER_RESET = "BREAKER_RESET"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    UNIVERSE_ADDED = "UNIVERSE_ADDED"
    UNIVERSE_REMOVED = "UNIVERSE_REMOVED"
    DATA_MISSING = "DATA_MISSING"
    RECONCILIATION = "RECONCILIATION"


class MetricScope(StrEnum):
    PORTFOLIO = "PORTFOLIO"
    DIRECTION = "DIRECTION"
    STRATEGY = "STRATEGY"
    ASSET = "ASSET"


class MetricStatus(StrEnum):
    OK = "OK"
    # Computable but on fewer observations than the documented minimum: shown, flagged.
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    # Not meaningful for this run (e.g. annualizing a 3-week run, a ratio with no losers).
    NOT_APPLICABLE = "NOT_APPLICABLE"


class BenchmarkKind(StrEnum):
    BENCHMARK = "BENCHMARK"  # e.g. SPY, buy at the first close, hold
    BUY_AND_HOLD = "BUY_AND_HOLD"  # equal-weight buy-and-hold of the run's universe


class BreakerResetPolicy(StrEnum):
    # No operator exists in a backtest: a manual breaker is reset by a simulated operator at
    # the next risk day's first decision once its condition is clear (recorded as such).
    NEXT_SESSION_IF_CLEAR = "NEXT_SESSION_IF_CLEAR"
    # A manual breaker stays tripped for the rest of the run.
    NEVER = "NEVER"


__all__ = [
    "FINAL_RUN_STATUSES",
    "BenchmarkKind",
    "BreakerResetPolicy",
    "EntryOrderType",
    "FeatureAdjustment",
    "MetricScope",
    "MetricStatus",
    "OrderRole",
    "ProposalOutcome",
    "RejectionStage",
    "RunEventKind",
    "RunStatus",
    "StopOrderType",
    "TradeStatus",
    "UniverseMode",
]

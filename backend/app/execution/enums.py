"""Enumerations of the paper execution engine (Phases 7A-7B). Stored as VARCHAR + CHECK.

Paper only: every order is a simulated order handled by the isolated paper broker
(spec 15.4, 24.1). Nothing here names or reaches a real brokerage.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    PROPOSED = "PROPOSED"  # inside the submission transaction only (never committed)
    APPROVED = "APPROVED"  # authorized, revalidated, reservation attached; awaiting the broker
    OPEN = "OPEN"  # acknowledged by the paper broker, working
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"  # cancellation requested, not yet confirmed
    CANCELLED = "CANCELLED"  # cancellation confirmed by the paper broker
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"  # a confirmed failure: nothing is or will be working for it


FINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
        OrderStatus.FAILED,
    }
)
# Orders the paper broker may be working (or about to work).
LIVE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.APPROVED,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    }
)


class OrderEventType(StrEnum):
    CREATED = "CREATED"  # -> PROPOSED
    APPROVED = "APPROVED"  # PROPOSED -> APPROVED
    REJECTED = "REJECTED"  # PROPOSED|APPROVED -> REJECTED
    ACCEPTED = "ACCEPTED"  # APPROVED -> OPEN (paper broker acknowledgement)
    FILL = "FILL"  # a simulated (in 7A: controlled test) fill
    CANCEL_REQUESTED = "CANCEL_REQUESTED"  # -> CANCEL_PENDING
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"  # CANCEL_PENDING -> CANCELLED
    CANCEL_REJECTED = "CANCEL_REJECTED"  # CANCEL_PENDING -> OPEN|PARTIALLY_FILLED
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    # Phase 7B: the paper broker's simulator triggered a stop (stop, stop-limit, trailing
    # stop). The status does not change; a triggered stop-limit may stay unfilled.
    TRIGGERED = "TRIGGERED"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"  # a limit order at the target that reduces a position (exit)
    TRAILING_STOP = "TRAILING_STOP"


LIMIT_PRICE_TYPES = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT})
STOP_PRICE_TYPES = frozenset({OrderType.STOP, OrderType.STOP_LIMIT})


class OrderSide(StrEnum):
    BUY = "BUY"  # open or increase a long
    SELL = "SELL"  # reduce or close a long
    SELL_SHORT = "SELL_SHORT"  # open or increase a short
    BUY_TO_COVER = "BUY_TO_COVER"  # reduce or close a short

    @property
    def buys(self) -> bool:
        return self in (OrderSide.BUY, OrderSide.BUY_TO_COVER)


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"  # opens exposure: needs an active reservation, consumes it on fills
    EXIT = "EXIT"  # only reduces the position an ENTRY order filled; consumes nothing


class OrderEventSource(StrEnum):
    EXECUTION_ENGINE = "EXECUTION_ENGINE"
    PAPER_BROKER = "PAPER_BROKER"
    OPERATOR = "OPERATOR"
    EXPIRY = "EXPIRY"
    RECOVERY = "RECOVERY"


class ExecutionDecision(StrEnum):
    """What a submission request produced."""

    ORDER_CREATED = "ORDER_CREATED"  # an order exists (it may itself be REJECTED)
    REFUSED = "REFUSED"  # unauthorized or invalid: no order was created


class OrderCommand(StrEnum):
    CANCEL = "CANCEL"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"  # gave up after the configured attempts; recovery handled it


class BrokerEventType(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FILL = "FILL"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    EXPIRED = "EXPIRED"
    TRIGGERED = "TRIGGERED"


class BrokerOrderStatus(StrEnum):
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


FINAL_BROKER_STATUSES = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.REJECTED,
    }
)


class BrokerEventSource(StrEnum):
    """Where a paper broker event came from.

    SYSTEM: the simulator's own bookkeeping responses (acknowledgement, rejection, cancel
    confirmation, expiry). CONTROLLED_TEST: an execution event injected by a test or an
    operator on a CONTROLLED_TEST account. SIMULATION (Phase 7B): a fill or trigger the
    simulator derived from stored market data under a versioned execution model; it is a
    simulated execution, never a brokerage execution.
    """

    SYSTEM = "SYSTEM"
    CONTROLLED_TEST = "CONTROLLED_TEST"
    SIMULATION = "SIMULATION"


class BrokerRunState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # accepts nothing new; work is retried later
    STOPPED = "STOPPED"  # shut down cleanly; recovery runs at the next start


class CancelMode(StrEnum):
    """How the controlled paper broker answers a cancellation request."""

    IMMEDIATE = "IMMEDIATE"  # confirm (or reject, when nothing is left) while handling it
    MANUAL = "MANUAL"  # acknowledge only; a controlled test event confirms later


class AttemptOutcome(StrEnum):
    """What one evaluation of a working order by the simulator decided (Phase 7B)."""

    FILLED = "FILLED"  # the remaining quantity filled
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # some filled, the rest keeps working
    TRIGGERED = "TRIGGERED"  # a stop triggered (it may fill in the same evaluation)
    NO_FILL = "NO_FILL"  # the data was sufficient and the order did not execute
    DEFERRED = "DEFERRED"  # insufficient, stale or closed-market data: nothing decided


class DataBasis(StrEnum):
    """Which market data a simulated fill price came from."""

    QUOTE = "QUOTE"  # the touch of a fresh bid/ask quote (marketable)
    TRADE_THROUGH = "TRADE_THROUGH"  # trades printed strictly through a resting limit
    BAR_THROUGH = "BAR_THROUGH"  # a completed bar traded strictly through a resting limit
    BAR_FALLBACK = "BAR_FALLBACK"  # no fresh quote: a completed bar plus an assumed spread


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PositionEventType(StrEnum):
    OPENED = "OPENED"  # first entry fill
    INCREASED = "INCREASED"  # later entry fill
    REDUCED = "REDUCED"  # exit fill (partial close)
    CLOSED = "CLOSED"  # nothing open and the entry order is final
    PROTECTION_SET = "PROTECTION_SET"  # stop / target / invalidation / expiry recorded
    PROTECTION_UPDATED = "PROTECTION_UPDATED"  # trailing stop ratchet or operator tightening
    TRIGGERED = "TRIGGERED"  # a protective level or the expiry was reached
    EXIT_SUBMITTED = "EXIT_SUBMITTED"  # the monitor submitted a protective/closing exit
    EXIT_ENDED = "EXIT_ENDED"  # that exit is final (filled, partly filled, expired...)
    ENTRY_CANCEL_REQUESTED = "ENTRY_CANCEL_REQUESTED"  # unfilled entry remainder withdrawn
    CLOSE_REQUESTED = "CLOSE_REQUESTED"  # operator close request recorded
    BORROW_ACCRUED = "BORROW_ACCRUED"  # short borrow cost accrued for a day
    COSTS_SETTLED = "COSTS_SETTLED"  # accrued borrow paid at close
    MONITOR_DEFERRED = "MONITOR_DEFERRED"  # a required price was missing or stale


class ExitReason(StrEnum):
    """Why a position (or part of it) was exited. Priority order on simultaneous triggers:
    STOP_LOSS/TRAILING_STOP, INVALIDATION, EXPIRATION, CLOSE_REQUEST, TAKE_PROFIT."""

    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    INVALIDATION = "INVALIDATION"
    EXPIRATION = "EXPIRATION"
    CLOSE_REQUEST = "CLOSE_REQUEST"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL_EXIT = "MANUAL_EXIT"  # an operator exit order outside the monitor


ADVERSE_EXIT_REASONS = frozenset(
    {ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP, ExitReason.INVALIDATION}
)


class CloseRequestStatus(StrEnum):
    PENDING = "PENDING"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    COMPLETED = "COMPLETED"
    REFUSED = "REFUSED"


__all__ = [
    "ADVERSE_EXIT_REASONS",
    "FINAL_BROKER_STATUSES",
    "FINAL_ORDER_STATUSES",
    "LIMIT_PRICE_TYPES",
    "LIVE_ORDER_STATUSES",
    "STOP_PRICE_TYPES",
    "AttemptOutcome",
    "BrokerEventSource",
    "BrokerEventType",
    "BrokerOrderStatus",
    "BrokerRunState",
    "CancelMode",
    "CloseRequestStatus",
    "DataBasis",
    "ExecutionDecision",
    "ExitReason",
    "OrderCommand",
    "OrderEventSource",
    "OrderEventType",
    "OrderPurpose",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "OutboxStatus",
    "PositionEventType",
    "PositionStatus",
]

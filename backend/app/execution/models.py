"""Request and result models of the paper execution engine (Phases 7A-7B)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.execution.enums import (
    ExecutionDecision,
    ExitReason,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.risk.config import StrictDecimal


def plain(value: Decimal) -> str:
    """A decimal for messages: no exponent, no trailing zeros ("92", "99.5", "0.01")."""
    text = format(value.normalize(), "f")
    return text


class OrderRequest(BaseModel):
    """A request to place a simulated paper order.

    ENTRY orders name the Phase 6C reservation they execute; EXIT orders name the ENTRY
    order whose filled position they reduce. Asset, side and originating proposal/decision
    may be declared; when they are, they must equal what the reservation records (they
    are never taken from the request on trust).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: uuid.UUID
    purpose: OrderPurpose = OrderPurpose.ENTRY
    reservation_id: uuid.UUID | None = None
    parent_order_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    side: OrderSide | None = None
    strategy_proposal_id: uuid.UUID | None = None
    ai_decision_id: uuid.UUID | None = None
    order_type: OrderType
    quantity: StrictDecimal
    limit_price: StrictDecimal | None = None
    stop_price: StrictDecimal | None = None
    trail_amount: StrictDecimal | None = None
    trail_percent: StrictDecimal | None = None
    expires_at: datetime | None = None
    # Phase 7B: why an EXIT is placed (set by the position monitor; operators may omit it).
    exit_reason: ExitReason | None = None

    def canonical(self) -> dict[str, Any]:
        """The request as hashed and stored. ``exit_reason`` is left out when unset, so a
        request made before Phase 7B hashes as it did then (idempotency replays still match)."""
        dump = self.model_dump(mode="json")
        if dump.get("exit_reason") is None:
            dump.pop("exit_reason", None)
        return dump


@dataclass(frozen=True)
class ExecutionConfig:
    """Execution engine constants (no environment variable; documented in D-160)."""

    default_order_lifetime: timedelta = timedelta(days=1)
    max_order_lifetime: timedelta = timedelta(days=7)
    outbox_max_attempts: int = 5
    outbox_base_backoff: timedelta = timedelta(seconds=5)
    # Phase 7B: lifetime of exits the position monitor places (a resting take-profit is
    # re-placed when it expires; a protective exit that expires unfilled is resubmitted).
    protective_order_lifetime: timedelta = timedelta(days=1)


@dataclass(frozen=True)
class SubmissionResult:
    request_id: uuid.UUID
    decision: ExecutionDecision
    order_id: uuid.UUID | None
    status: OrderStatus | None  # the order's current status (None when refused)
    reasons: list[dict[str, Any]]
    created: bool  # False when an earlier request with the same key is returned


@dataclass(frozen=True)
class OrderCommandResult:
    order_id: uuid.UUID
    status: OrderStatus
    created: bool


@dataclass
class ExpiryReport:
    expired: list[uuid.UUID] = field(default_factory=list)
    sent_to_broker: list[uuid.UUID] = field(default_factory=list)


@dataclass
class RecoveryReport:
    broker_working_orders: int = 0
    live_orders_checked: int = 0
    broker_events_applied: int = 0
    failed_unsubmitted: list[uuid.UUID] = field(default_factory=list)
    cancellations_retried: list[uuid.UUID] = field(default_factory=list)
    expired: list[uuid.UUID] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class ControlledFill(BaseModel):
    """A CONTROLLED TEST execution event (Phase 7A has no fill algorithm)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: uuid.UUID
    quantity: StrictDecimal = Field(gt=0)
    price: StrictDecimal = Field(gt=0)
    commission: StrictDecimal = Field(default=Decimal(0), ge=0)
    fees: StrictDecimal = Field(default=Decimal(0), ge=0)
    external_ref: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9._:@/-]+$")


__all__ = [
    "ControlledFill",
    "ExecutionConfig",
    "ExpiryReport",
    "OrderCommandResult",
    "OrderRequest",
    "RecoveryReport",
    "SubmissionResult",
    "plain",
]

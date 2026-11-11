"""The order state machine (spec 15.1; Phases 7A-7B). The single source of legal transitions.

``TRANSITIONS`` maps ``(from_status, event_type)`` to the statuses the event may lead to.
Python checks every transition here before writing it; the database trigger
``atlas_order_event_apply`` (migrations 0016, 0017) enforces the same table, so a transition
that skips this module is still refused. ``test_order_state_machine`` compares the two.

Quantity-dependent rules (which of two targets an event leads to):

* FILL: ``FILLED`` exactly when the fill takes the whole remaining quantity, otherwise
  ``PARTIALLY_FILLED`` (or stays ``CANCEL_PENDING`` while a cancellation is pending);
* CANCEL_REJECTED: back to ``OPEN`` when nothing was filled, ``PARTIALLY_FILLED`` otherwise.

Final statuses (FILLED, CANCELLED, EXPIRED, REJECTED, FAILED) have no transition. A
cancellation is final only when the paper broker confirms it (CANCEL_CONFIRMED); until then
the order is CANCEL_PENDING, may still fill, and its reservation is not released.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.exceptions import AtlasError
from app.execution.enums import FINAL_ORDER_STATUSES, OrderEventType, OrderStatus

S = OrderStatus
E = OrderEventType

TRANSITIONS: dict[tuple[OrderStatus | None, OrderEventType], frozenset[OrderStatus]] = {
    (None, E.CREATED): frozenset({S.PROPOSED}),
    (S.PROPOSED, E.APPROVED): frozenset({S.APPROVED}),
    (S.PROPOSED, E.REJECTED): frozenset({S.REJECTED}),
    (S.APPROVED, E.ACCEPTED): frozenset({S.OPEN}),
    (S.APPROVED, E.REJECTED): frozenset({S.REJECTED}),
    (S.APPROVED, E.CANCEL_REQUESTED): frozenset({S.CANCEL_PENDING}),
    (S.APPROVED, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.APPROVED, E.FAILED): frozenset({S.FAILED}),
    (S.OPEN, E.FILL): frozenset({S.PARTIALLY_FILLED, S.FILLED}),
    (S.OPEN, E.CANCEL_REQUESTED): frozenset({S.CANCEL_PENDING}),
    (S.OPEN, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.OPEN, E.FAILED): frozenset({S.FAILED}),
    (S.PARTIALLY_FILLED, E.FILL): frozenset({S.PARTIALLY_FILLED, S.FILLED}),
    (S.PARTIALLY_FILLED, E.CANCEL_REQUESTED): frozenset({S.CANCEL_PENDING}),
    (S.PARTIALLY_FILLED, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.PARTIALLY_FILLED, E.FAILED): frozenset({S.FAILED}),
    # The broker acknowledged an order whose cancellation was already requested: the
    # acknowledgement is recorded, the cancellation stays pending.
    (S.CANCEL_PENDING, E.ACCEPTED): frozenset({S.CANCEL_PENDING}),
    (S.CANCEL_PENDING, E.FILL): frozenset({S.CANCEL_PENDING, S.FILLED}),
    (S.CANCEL_PENDING, E.CANCEL_CONFIRMED): frozenset({S.CANCELLED}),
    (S.CANCEL_PENDING, E.CANCEL_REJECTED): frozenset({S.OPEN, S.PARTIALLY_FILLED}),
    (S.CANCEL_PENDING, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.CANCEL_PENDING, E.FAILED): frozenset({S.FAILED}),
    # Phase 7B: a stop (stop, stop-limit, trailing stop) triggered at the paper broker.
    # Recorded for the audit trail; the status is unchanged (a stop fills only after its
    # trigger, so it is never partially filled before one).
    (S.OPEN, E.TRIGGERED): frozenset({S.OPEN}),
    (S.CANCEL_PENDING, E.TRIGGERED): frozenset({S.CANCEL_PENDING}),
}

# Statuses in which the paper broker may fill an order.
FILLABLE = frozenset({S.OPEN, S.PARTIALLY_FILLED, S.CANCEL_PENDING})
# Statuses from which a cancellation can be requested.
CANCELLABLE = frozenset({S.APPROVED, S.OPEN, S.PARTIALLY_FILLED})


class InvalidOrderTransitionError(AtlasError):
    """The requested order transition is not legal. Nothing was changed."""

    code = "invalid_order_transition"
    http_status = 409


def allowed_targets(status: OrderStatus | None, event: OrderEventType) -> frozenset[OrderStatus]:
    return TRANSITIONS.get((status, event), frozenset())


def is_allowed(status: OrderStatus | None, event: OrderEventType, target: OrderStatus) -> bool:
    return target in allowed_targets(status, event)


def is_final(status: OrderStatus) -> bool:
    return status in FINAL_ORDER_STATUSES


def next_status(
    status: OrderStatus | None,
    event: OrderEventType,
    *,
    quantity: Decimal,
    filled: Decimal,
    fill: Decimal = Decimal(0),
) -> OrderStatus:
    """The status ``event`` leads to from ``status`` for an order of ``quantity`` with
    ``filled`` already filled (``fill``: this event's fill quantity). Raises
    :class:`InvalidOrderTransitionError` when the event is not legal here."""
    targets = allowed_targets(status, event)
    label = "(new)" if status is None else status.value
    if not targets:
        raise InvalidOrderTransitionError(f"{event.value} is not a legal transition from {label}")
    if event is E.FILL:
        remaining = quantity - filled
        if fill <= 0:
            raise InvalidOrderTransitionError("a fill must have a positive quantity")
        if fill > remaining:
            raise InvalidOrderTransitionError(
                f"a fill of {fill} exceeds the remaining {remaining} (an over-fill)"
            )
        if fill == remaining:
            return S.FILLED
        return S.CANCEL_PENDING if status is S.CANCEL_PENDING else S.PARTIALLY_FILLED
    if event is E.CANCEL_REJECTED:
        return S.OPEN if filled == 0 else S.PARTIALLY_FILLED
    (only,) = targets
    return only


def check(status: OrderStatus | None, event: OrderEventType, target: OrderStatus) -> None:
    """Refuse ``status --event--> target`` unless the table allows it."""
    if not is_allowed(status, event, target):
        label = "(new)" if status is None else status.value
        raise InvalidOrderTransitionError(
            f"{label} --{event.value}--> {target.value} is not a legal order transition"
        )


__all__ = [
    "CANCELLABLE",
    "FILLABLE",
    "TRANSITIONS",
    "InvalidOrderTransitionError",
    "allowed_targets",
    "check",
    "is_allowed",
    "is_final",
    "next_status",
]

"""The paper broker interface (spec 15.4, 24.1; Phase 7A).

This is the **only** execution pathway in Atlas, and it is simulated. It is separate from
the Alpaca market-data adapter (``app.data_engine``): nothing here imports, configures or
calls a provider, and no implementation may connect to a brokerage trading endpoint. A
static test (``test_execution_isolation``) checks the imports.

Every method runs inside the caller's database transaction: the simulator's book
(``paper_broker_orders``) and its events (``paper_broker_events``) are PostgreSQL rows, so
an order's state change and the simulator's response commit or roll back together, and a
restart loses nothing.

Fill algorithms are Phase 7B. In Phase 7A fills exist only as labelled controlled test
events (the database refuses any other FILL source).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core.exceptions import AtlasError
from app.database.models.execution import OrderRecord, PaperBrokerEventRecord
from app.execution.enums import BrokerOrderStatus, BrokerRunState


class BrokerUnavailableError(AtlasError):
    """The paper broker is paused or stopped: the work is retried later (nothing changed)."""

    code = "paper_broker_unavailable"
    http_status = 503


class BrokerRefusedError(AtlasError):
    """The paper broker refused a request (e.g. an over-fill of its book). Nothing changed."""

    code = "paper_broker_refused"
    http_status = 409


@dataclass(frozen=True)
class BrokerOrderView:
    broker_order_id: uuid.UUID
    client_order_id: uuid.UUID
    status: BrokerOrderStatus
    quantity: Decimal
    filled_quantity: Decimal
    last_event_sequence: int
    expires_at: datetime


@dataclass(frozen=True)
class BrokerHealth:
    broker_id: str
    simulated: bool  # always True
    status: BrokerRunState
    accepting_orders: bool
    fill_model: str
    last_heartbeat_at: datetime | None
    last_recovery_at: datetime | None
    working_orders: int
    details: dict[str, Any]


@dataclass(frozen=True)
class BrokerRecovery:
    broker_id: str
    working_orders: list[uuid.UUID]  # client order ids the simulator still works


class PaperBroker(Protocol):
    """What the execution engine needs from a (simulated) broker."""

    broker_id: str

    # --------------------------------------------------------------- orders
    def submit_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord:
        """Accept or reject ``order``. A second submission of the same client order records
        nothing new and returns the original answer (duplicate suppression)."""
        ...

    def cancel_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord | None:
        """Request cancellation. Returns the confirmation/rejection event, or None when the
        simulator only acknowledged it (confirmation follows as a later event)."""
        ...

    def expire_order(
        self, session: Session, order: OrderRecord, at: datetime
    ) -> PaperBrokerEventRecord | None:
        """Expire a working order that is due; None when it is not working or not due."""
        ...

    def order_status(self, session: Session, client_order_id: uuid.UUID) -> BrokerOrderView | None:
        """The simulator's view of an order (None: it never received it)."""
        ...

    def events_after(
        self, session: Session, client_order_id: uuid.UUID, sequence: int
    ) -> list[PaperBrokerEventRecord]:
        """The simulator's events for an order after ``sequence``, in order."""
        ...

    # --------------------------------------------------------------- lifecycle
    def health(self, session: Session) -> BrokerHealth: ...

    def start(self, session: Session, at: datetime) -> BrokerRecovery:
        """Start (or restart): mark RUNNING and report the orders still being worked."""
        ...

    def pause(self, session: Session, at: datetime, reason: str) -> None: ...

    def heartbeat(self, session: Session, at: datetime) -> None: ...

    def shutdown(self, session: Session, at: datetime) -> None:
        """Stop cleanly: nothing is lost (every order and event is already a row)."""
        ...


__all__ = [
    "BrokerHealth",
    "BrokerOrderView",
    "BrokerRecovery",
    "BrokerRefusedError",
    "BrokerUnavailableError",
    "PaperBroker",
]

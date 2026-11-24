"""The execution -> portfolio/accounting integration contract (spec 13, 16; Phases 7A-7B).

A fill never updates positions or cash directly. It becomes **one 6A posting** through
the existing posting service, inside the transaction that applies the fill:

| Order side     | 6A template      | Asset quantity movement |
|----------------|------------------|-------------------------|
| BUY            | ``long_purchase`` | +quantity              |
| SELL           | ``long_sale``     | -quantity              |
| SELL_SHORT     | ``short_open``    | -quantity              |
| BUY_TO_COVER   | ``short_cover``   | +quantity              |

The 6A templates already maintain asset quantities, position state, cash, average cost
basis, realized P/L (on reductions) and transaction costs (commission and fees are passed
through); unrealized valuation stays a 6A valuation checkpoint. Nothing here duplicates
that logic: this module only chooses the template, the source and the idempotency key.

Source: the posting's 6A source follows the fill's paper broker source, never a setting:
a controlled test event posts as ``CONTROLLED_TEST`` (6A accepts it only on CONTROLLED_TEST
accounts); a simulated fill (Phase 7B) posts as ``SIMULATED_EXECUTION``, which 6A accepts
for the trade and cost templates on any paper account. There is no other mapping.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.accounting.enums import EntrySource
from app.accounting.posting import PostingRequest, PostingResult, PostingService
from app.execution.enums import BrokerEventSource, OrderSide

TEMPLATES: dict[OrderSide, str] = {
    OrderSide.BUY: "long_purchase",
    OrderSide.SELL: "long_sale",
    OrderSide.SELL_SHORT: "short_open",
    OrderSide.BUY_TO_COVER: "short_cover",
}
# The 6A source of each fill source (a SYSTEM broker event never fills: database CHECK).
SOURCES: dict[BrokerEventSource, EntrySource] = {
    BrokerEventSource.CONTROLLED_TEST: EntrySource.CONTROLLED_TEST,
    BrokerEventSource.SIMULATION: EntrySource.SIMULATED_EXECUTION,
}
# The signed asset-quantity movement each side posts (checked by the database too).
MOVEMENT_SIGN: dict[OrderSide, int] = {
    OrderSide.BUY: 1,
    OrderSide.SELL: -1,
    OrderSide.SELL_SHORT: -1,
    OrderSide.BUY_TO_COVER: 1,
}


@dataclass(frozen=True)
class FillPosting:
    """What a fill asks the books to record (asset currency amounts)."""

    account_id: uuid.UUID
    order_id: uuid.UUID
    broker_event_id: uuid.UUID
    asset_id: uuid.UUID
    side: OrderSide
    quantity: Decimal
    price: Decimal
    commission: Decimal
    fees: Decimal
    filled_at: datetime
    source: BrokerEventSource = BrokerEventSource.CONTROLLED_TEST


class PositionLedger(Protocol):
    """The portfolio side of a fill: post it in the caller's transaction, exactly once."""

    def post_fill(self, session: Session, fill: FillPosting, *, actor: str) -> PostingResult: ...


class AccountingPositionLedger:
    """:class:`PositionLedger` backed by the Phase 6A posting service (the only one)."""

    def __init__(self, posting: PostingService) -> None:
        self._posting = posting

    @staticmethod
    def event_id(broker_event_id: uuid.UUID) -> str:
        return f"paper-fill:{broker_event_id}"

    def post_fill(self, session: Session, fill: FillPosting, *, actor: str) -> PostingResult:
        source = SOURCES.get(fill.source)
        if source is None:
            raise ValueError(f"a {fill.source.value} paper broker event is not a fill")
        return self._posting.post_in(
            session,
            PostingRequest(
                account_id=fill.account_id,
                template=TEMPLATES[fill.side],
                params={
                    "asset_id": str(fill.asset_id),
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "commission": str(fill.commission),
                    "fees": str(fill.fees),
                },
                event_id=self.event_id(fill.broker_event_id),
                effective_at=fill.filled_at,
                source=source,
                actor=actor,
                source_ref=f"order:{fill.order_id}",
            ),
        )


__all__ = [
    "MOVEMENT_SIGN",
    "SOURCES",
    "TEMPLATES",
    "AccountingPositionLedger",
    "FillPosting",
    "PositionLedger",
]

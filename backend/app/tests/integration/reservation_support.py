"""Shared setup for the Phase 6C reservation integration tests.

TEST FIXTURE material only (see ``risk_support``): CONTROLLED_TEST accounts with simulated
capital, invented instruments with MOCK bars, fixture liquidity and classifications. Fills
are **controlled test events**: a 6A ``long_purchase`` / ``short_open`` posting with source
CONTROLLED_TEST, consumed in the same transaction. No broker, order or execution exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.accounting.config import AccountingSettings
from app.accounting.enums import EntrySource
from app.accounting.posting import PostingRequest
from app.database.models.reservations import (
    RiskReservationEventRecord,
    RiskReservationRecord,
    RiskReservationStateRecord,
)
from app.database.sessions import session_scope
from app.risk.enums import ReservationStatus, TradeDirection
from app.risk.models import Proposal
from app.risk.reservations.reconciliation import ReservationReconciler
from app.risk.reservations.service import CommandResult, ReservationResult, ReservationService
from app.tests.integration.accounting_support import NOW
from app.tests.integration.risk_support import Risk, make_risk

TEMPLATES = {TradeDirection.LONG: "long_purchase", TradeDirection.SHORT: "short_open"}


@dataclass
class Reservations:
    risk: Risk
    service: ReservationService
    reconciler: ReservationReconciler

    @property
    def factory(self) -> Any:
        return self.risk.factory

    def reserve(
        self, account: uuid.UUID, proposal: Proposal, key: str, by: str = "system:test-worker"
    ) -> ReservationResult:
        return self.service.reserve(account, proposal, idempotency_key=key, requested_by=by)

    def fill(
        self,
        reservation_id: uuid.UUID,
        quantity: str,
        price: str,
        key: str,
        *,
        order_ref: str = "test-order",
    ) -> CommandResult:
        """A controlled test fill: post the 6A trade and consume the reservation in ONE
        transaction (how Phase 7 will do it), so exposure moves from reserved to the ledger
        atomically."""
        with session_scope(self.factory) as session:
            r = session.get(RiskReservationRecord, reservation_id)
            assert r is not None
            posted = self.risk.books.ctx.posting.post_in(
                session,
                PostingRequest(
                    account_id=r.portfolio_account_id,
                    template=TEMPLATES[r.direction],
                    params={
                        "asset_id": str(r.asset_id),
                        "quantity": quantity,
                        "price": price,
                        "commission": "0",
                        "fees": "0",
                    },
                    event_id=f"fill:{key}",
                    effective_at=self.risk.now(),
                    source=EntrySource.CONTROLLED_TEST,
                    actor="test",
                ),
            )
            return self.service.consume_in(
                session,
                reservation_id,
                quantity=Decimal(quantity),
                journal_entry_id=posted.entry_id,
                idempotency_key=f"consume:{key}",
                actor="system:test-execution",
                order_ref=order_ref,
            )

    def attach(self, reservation_id: uuid.UUID, order_ref: str = "test-order") -> CommandResult:
        return self.service.attach_order(
            reservation_id,
            order_ref=order_ref,
            idempotency_key=f"attach:{reservation_id}",
            actor="system:test-execution",
        )

    def get(self, reservation_id: uuid.UUID) -> RiskReservationRecord:
        with session_scope(self.factory) as session:
            r = session.get(RiskReservationRecord, reservation_id)
            assert r is not None
            session.expunge(r)
            return r

    def state(self, account: uuid.UUID) -> RiskReservationStateRecord:
        with session_scope(self.factory) as session:
            s = session.get(RiskReservationStateRecord, account)
            if s is None:  # nothing was ever committed for the account
                zero = Decimal(0)
                return RiskReservationStateRecord(
                    portfolio_account_id=account,
                    last_sequence=0,
                    active_count=0,
                    reserved_risk=zero,
                    reserved_exposure=zero,
                    reserved_long_exposure=zero,
                    reserved_short_exposure=zero,
                    reserved_capital=zero,
                    reserved_margin=zero,
                )
            session.expunge(s)
            return s

    def events(self, reservation_id: uuid.UUID) -> list[RiskReservationEventRecord]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                select(RiskReservationEventRecord)
                .where(RiskReservationEventRecord.reservation_id == reservation_id)
                .order_by(RiskReservationEventRecord.reservation_sequence)
            ).all()
            session.expunge_all()
            return list(rows)

    def count(self, session: Session, status: ReservationStatus | None = None) -> int:
        q = select(func.count()).select_from(RiskReservationRecord)
        if status is not None:
            q = q.where(RiskReservationRecord.status == status)
        return int(session.scalar(q) or 0)


def make_reservations(engine: Engine, now: Any = NOW) -> Reservations:
    risk = make_risk(engine, now)
    service = ReservationService(
        risk.factory, risk.clock, AccountingSettings(_env_file=None), risk.service
    )
    return Reservations(risk, service, ReservationReconciler(risk.factory, risk.clock))


__all__ = ["Reservations", "make_reservations"]

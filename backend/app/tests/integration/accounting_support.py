"""Shared setup for the Phase 6A accounting integration tests.

Everything here is TEST FIXTURE material: invented instruments, MOCK unadjusted daily bars
(``strategy_support.seed_bars``), TEST_FIXTURE FX rates and CONTROLLED_TEST accounts. None
of it is market data, and no order is executed anywhere.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.accounting.enums import EntrySource, FxRateSource, InstrumentSpecSource
from app.accounting.posting import PostingRequest, PostingResult
from app.accounting.reference import record_fx_rate, register_instrument_spec
from app.accounting.runtime import AccountingContext, build_accounting_context
from app.core.clocks import FixedClock
from app.data_engine.enums import PriceAdjustment
from app.database.models import AssetClass, PortfolioAccount
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.database.sessions import build_session_factory, session_scope
from app.portfolio.accounts import open_and_fund
from app.tests.integration.strategy_support import make_asset, seed_bars

# Tuesday 3 November 2026, 22:30 UTC: after the NYSE close, the session's bar is the latest.
NOW = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
SPEC_FROM = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass
class Books:
    factory: sessionmaker[Session]
    clock: FixedClock
    ctx: AccountingContext

    def account(self, name: str, *, test: bool = True, funding: str = "100000.00") -> uuid.UUID:
        with session_scope(self.factory) as session:
            funded = open_and_fund(
                session,
                self.ctx.posting,
                name=name,
                purpose=PortfolioAccountPurpose.CONTROLLED_TEST
                if test
                else PortfolioAccountPurpose.PRIMARY,
                base_currency="USD",
                amount=Decimal(funding),
                at=self.clock.now(),
                actor="test",
            )
            return funded.account.id

    def asset(
        self,
        symbol: str,
        *,
        increment: str = "1",
        asset_class: AssetClass = AssetClass.EQUITY,
        bars: tuple[float, float] | None = None,
        bars_as_of: datetime = NOW,
    ) -> uuid.UUID:
        asset_id = make_asset(self.factory, symbol, asset_class)
        with session_scope(self.factory) as session:
            register_instrument_spec(
                session,
                asset_id=asset_id,
                quantity_increment=Decimal(increment),
                min_quantity=Decimal(increment),
                valid_from=SPEC_FROM,
                source=InstrumentSpecSource.OPERATOR,
                provenance="test fixture lot size",
                created_by="test",
            )
        if bars is not None:
            self.bars(asset_id, *bars, as_of=bars_as_of)
        return asset_id

    def bars(
        self, asset_id: uuid.UUID, start: float, step: float, *, as_of: datetime = NOW
    ) -> None:
        """MOCK unadjusted daily bars; with step 0 the last (even-index) close is start - 0.3."""
        seed_bars(
            self.factory,
            asset_id,
            as_of=as_of,
            count=5,
            start=start,
            step=step,
            adjustment=PriceAdjustment.RAW,
        )

    def rate(
        self,
        base: str,
        rate: str,
        *,
        at: datetime | None = None,
        source: FxRateSource = FxRateSource.TEST_FIXTURE,
    ) -> int:
        observed = at or self.clock.now()
        with session_scope(self.factory) as session:
            return record_fx_rate(
                session,
                base=base,
                quote="USD",
                rate=Decimal(rate),
                observed_at=observed,
                source=source,
                provenance="test fixture rate",
                recorded_at=max(observed, self.clock.now()),
                created_by="test",
            ).id

    def post(
        self,
        account_id: uuid.UUID,
        template: str,
        event_id: str,
        params: dict[str, Any],
        *,
        source: EntrySource = EntrySource.CONTROLLED_TEST,
        at: datetime | None = None,
    ) -> PostingResult:
        return self.ctx.posting.post(
            PostingRequest(
                account_id=account_id,
                template=template,
                params=params,
                event_id=event_id,
                effective_at=at or self.clock.now(),
                source=source,
                actor="test",
            )
        )

    def trade(
        self,
        account_id: uuid.UUID,
        template: str,
        event_id: str,
        asset_id: uuid.UUID,
        quantity: str,
        price: str,
        commission: str = "0",
        fees: str = "0",
    ) -> PostingResult:
        return self.post(
            account_id,
            template,
            event_id,
            {
                "asset_id": str(asset_id),
                "quantity": quantity,
                "price": price,
                "commission": commission,
                "fees": fees,
            },
        )

    def get_account(self, account_id: uuid.UUID) -> PortfolioAccount:
        with session_scope(self.factory) as session:
            found = session.get(PortfolioAccount, account_id)
            assert found is not None
            return found


def make_books(engine: Engine, now: datetime = NOW) -> Books:
    factory = build_session_factory(engine)
    clock = FixedClock(now)
    ctx = build_accounting_context(factory, clock, settings=AccountingSettings(_env_file=None))
    return Books(factory, clock, ctx)


def later(books: Books, minutes: int) -> datetime:
    books.clock.advance(timedelta(minutes=minutes))
    return books.clock.now()

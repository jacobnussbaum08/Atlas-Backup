"""Shared setup for the Phase 6B risk integration tests.

TEST FIXTURE material only: CONTROLLED_TEST accounts funded with simulated capital (Phase
6A), invented instruments with MOCK unadjusted daily bars, FIXTURE provider reference
versions, invented liquidity measurements and operator-style classifications. Nothing here
is market data and nothing is executed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.asset_discovery.enums import MetricStatus
from app.core.clocks import FixedClock
from app.data_engine.enums import PriceAdjustment
from app.database.models import AssetMetricValueRecord
from app.database.models.risk import RiskAssetClassificationRecord
from app.database.sessions import session_scope
from app.risk import governance
from app.risk.enums import ClassificationSource, ProposalSource, TradeDirection
from app.risk.models import Proposal
from app.risk.service import AssessmentRecordResult, RiskService
from app.tests.integration.accounting_support import NOW, SPEC_FROM, Books, make_books
from app.tests.integration.strategy_support import seed_bars

OPERATOR = "operator:test"
LONG, SHORT = TradeDirection.LONG, TradeDirection.SHORT


@dataclass
class Risk:
    books: Books
    service: RiskService

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.books.factory

    @property
    def clock(self) -> FixedClock:
        return self.books.clock

    def now(self) -> datetime:
        return self.clock.now()

    # ---------------------------------------------------------------- accounts
    def account(self, name: str = "risk-test", *, configure: bool = True) -> uuid.UUID:
        account_id = self.books.account(name)
        if configure:
            self.configure(account_id)
        self.reconcile(account_id)
        return account_id

    def configure(self, account_id: uuid.UUID, **limits: Any) -> None:
        with session_scope(self.factory) as session:
            governance.initialize(
                session,
                account_id,
                actor=OPERATOR,
                reason="test configuration with the documented starting values",
                at=self.now(),
            )
            if limits:
                governance.change(
                    session,
                    account_id,
                    actor=OPERATOR,
                    reason="test-specific hard limits",
                    at=self.now(),
                    hard_limit_updates=limits,
                )

    def reconcile(self, account_id: uuid.UUID) -> str:
        return self.books.ctx.reconciliation.run(account_id, actor="test").status.value

    # ---------------------------------------------------------------- instruments
    def asset(
        self,
        symbol: str,
        *,
        start: float = 100.0,
        bars_as_of: datetime = NOW,
        adv: str | None = "50000000",
        sector: str | None = None,
        cluster: str | None = None,
        increment: str = "1",
    ) -> uuid.UUID:
        asset_id = self.books.asset(
            symbol, increment=increment, bars=(start, 0.0), bars_as_of=bars_as_of
        )
        if adv is not None:
            self.adv(asset_id, adv)
        if sector is not None or cluster is not None:
            self.classify(asset_id, sector=sector, cluster=cluster)
        return asset_id

    def bar(
        self, asset_id: uuid.UUID, close_plus_03: float, *, session_close_as_of: datetime
    ) -> None:
        """One MOCK daily bar for the session completed by ``session_close_as_of`` (minus the
        one-hour settle), available at that session's close. Its close is ``value - 0.3``."""
        seed_bars(
            self.factory,
            asset_id,
            as_of=session_close_as_of,
            count=1,
            start=close_plus_03,
            step=0.0,
            available_after_close=timedelta(0),
            adjustment=PriceAdjustment.RAW,
        )

    def adv(self, asset_id: uuid.UUID, value: str, as_of: datetime | None = None) -> None:
        at = as_of or NOW - timedelta(hours=1)
        with session_scope(self.factory) as session:
            session.add(
                AssetMetricValueRecord(
                    asset_id=asset_id,
                    metric="median_dollar_volume",
                    window="20 sessions",
                    timeframe="1Day",
                    provider="alpaca",
                    feed="iex",
                    feature_version="fixture",
                    as_of=at,
                    source_start=at - timedelta(days=30),
                    source_end=at,
                    observations=20,
                    expected_observations=20,
                    value=Decimal(value),
                    status=MetricStatus.OK,
                    details={"fixture": True},
                )
            )

    def classify(
        self, asset_id: uuid.UUID, *, sector: str | None, cluster: str | None = None
    ) -> None:
        with session_scope(self.factory) as session:
            session.add(
                RiskAssetClassificationRecord(
                    asset_id=asset_id,
                    sector=sector,
                    cluster=cluster,
                    valid_from=SPEC_FROM,
                    source=ClassificationSource.TEST_FIXTURE,
                    provenance="test fixture classification",
                    created_by="test",
                )
            )

    # ---------------------------------------------------------------- assessments
    def proposal(
        self,
        asset_id: uuid.UUID,
        entry: str,
        stop: str | None,
        direction: TradeDirection = LONG,
        **extra: Any,
    ) -> Proposal:
        return Proposal(
            source=ProposalSource.OPERATOR,
            asset_id=asset_id,
            direction=direction,
            entry=Decimal(entry),
            stop=None if stop is None else Decimal(stop),
            **extra,
        )

    def assess(
        self, account_id: uuid.UUID, proposal: Proposal, key: str | None = None
    ) -> AssessmentRecordResult:
        return self.service.assess(account_id, proposal, requested_by="test", request_key=key)


def make_risk(engine: Engine, now: datetime = NOW) -> Risk:
    books = make_books(engine, now)
    service = RiskService(books.factory, books.clock, AccountingSettings(_env_file=None))
    return Risk(books, service)


__all__ = ["LONG", "NOW", "OPERATOR", "SHORT", "Risk", "make_risk"]

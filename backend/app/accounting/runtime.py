"""Wiring of the accounting and portfolio services for the API and the CLI (Phase 6A)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings, get_accounting_settings
from app.accounting.posting import PostingService
from app.accounting.reconciliation import ReconciliationService
from app.core.clocks import Clock
from app.portfolio.valuation import ValuationService


@dataclass(frozen=True)
class AccountingContext:
    settings: AccountingSettings
    session_factory: sessionmaker[Session]
    clock: Clock
    posting: PostingService
    valuation: ValuationService
    reconciliation: ReconciliationService


def build_accounting_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    *,
    settings: AccountingSettings | None = None,
) -> AccountingContext:
    settings = settings or get_accounting_settings()
    posting = PostingService(session_factory, clock, settings)
    return AccountingContext(
        settings=settings,
        session_factory=session_factory,
        clock=clock,
        posting=posting,
        valuation=ValuationService(session_factory, clock, settings, posting),
        reconciliation=ReconciliationService(session_factory, clock),
    )


__all__ = ["AccountingContext", "build_accounting_context"]

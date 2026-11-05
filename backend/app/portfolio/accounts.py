"""Portfolio accounts: creation and simulated opening funding (spec 2, 13; decision D-125).

The PRIMARY account is the simulated $100,000 USD paper account. Its opening funding is
posted once (event ``opening-funding``) as contributed capital, never as a return.
CONTROLLED_TEST accounts hold controlled test transactions; the primary account never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.enums import EntrySource, EntryType
from app.accounting.money import currency
from app.accounting.posting import PostingError, PostingRequest, PostingResult, PostingService
from app.accounting.templates import OpeningFundingParams
from app.database.models import ActorType, PortfolioAccount
from app.database.models.accounting import JournalEntryRecord
from app.database.models.portfolio_account import (
    PortfolioAccountPurpose,
    PortfolioAccountStatus,
    PortfolioAccountType,
)
from app.database.repositories.audit_log import AuditLogRepository

OPENING_FUNDING_EVENT = "opening-funding"


@dataclass(frozen=True)
class FundedAccount:
    account: PortfolioAccount
    created: bool
    funding: PostingResult


def get_or_create_account(
    session: Session,
    *,
    name: str,
    purpose: PortfolioAccountPurpose,
    base_currency: str,
    actor: str,
    description: str | None = None,
) -> tuple[PortfolioAccount, bool]:
    base_currency = currency(base_currency).code
    found = session.scalar(select(PortfolioAccount).where(PortfolioAccount.name == name))
    if found is not None:
        if found.purpose is not purpose or found.base_currency != base_currency:
            raise PostingError(
                f"account {name!r} exists as {found.purpose.value} in {found.base_currency}"
            )
        return found, False
    account = PortfolioAccount(
        name=name,
        account_type=PortfolioAccountType.PAPER,
        base_currency=base_currency,
        purpose=purpose,
        status=PortfolioAccountStatus.ACTIVE,
        description=description,
    )
    session.add(account)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=ActorType.USER if actor.startswith("operator") else ActorType.SUBSYSTEM,
        actor_id=actor,
        action="portfolio.account_created",
        entity_type="portfolio_account",
        entity_id=str(account.id),
        new_state={"name": name, "purpose": purpose.value, "base_currency": base_currency},
    )
    return account, True


def open_and_fund(
    session: Session,
    posting: PostingService,
    *,
    name: str,
    purpose: PortfolioAccountPurpose,
    base_currency: str,
    amount: Decimal,
    at: datetime,
    actor: str,
    description: str | None = None,
) -> FundedAccount:
    """Create the account if needed and post its opening funding (idempotent)."""
    account, created = get_or_create_account(
        session,
        name=name,
        purpose=purpose,
        base_currency=base_currency,
        actor=actor,
        description=description,
    )
    # Funding happens once. Re-running is a no-op whatever the time; a different amount or
    # currency is refused, never posted as a second funding.
    existing = session.scalar(
        select(JournalEntryRecord).where(
            JournalEntryRecord.portfolio_account_id == account.id,
            JournalEntryRecord.entry_type == EntryType.OPENING_FUNDING,
        )
    )
    if existing is not None:
        stored = (Decimal(str(existing.payload["amount"])), existing.payload["currency"])
        if stored != (amount, base_currency):
            raise PostingError(
                f"account {name!r} was funded with {stored[0]} {stored[1]}; funding is never "
                "repeated or changed"
            )
        return FundedAccount(
            account,
            created,
            PostingResult(existing.id, existing.sequence, existing.entry_type, created=False),
        )
    funding = posting.post_in(
        session,
        PostingRequest(
            account_id=account.id,
            template="opening_funding",
            params=OpeningFundingParams(amount=amount, currency=base_currency),
            event_id=OPENING_FUNDING_EVENT,
            effective_at=at,
            source=EntrySource.SYSTEM,
            actor=actor,
            source_ref="simulated opening funding",
        ),
    )
    return FundedAccount(account, created, funding)


__all__ = ["OPENING_FUNDING_EVENT", "FundedAccount", "get_or_create_account", "open_and_fund"]

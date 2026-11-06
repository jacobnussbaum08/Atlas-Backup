"""Active reservations as reserved exposure for the risk view (Phase 6C).

Each APPROVED or PARTIALLY_CONSUMED reservation contributes its *unconsumed remainder*:
the consumed part is already a ledger position (consumption requires the ledger fill) and
is counted there, so nothing is counted twice. Amounts are the approval-time functional
amounts kept by the projection; for a foreign instrument they are scaled up when the quote
currency has strengthened since approval (never down), and a reservation whose currency has
no fresh rate is reported as an issue, which makes the assessment INSUFFICIENT_DATA.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.config import AccountingSettings
from app.accounting.enums import FxRateSource, PriceStatus
from app.accounting.money import ONE, currency, div, money_context
from app.accounting.reference import functional_rate
from app.database.models.reservations import RiskReservationRecord
from app.risk.data import classification_at
from app.risk.enums import ACTIVE_RESERVATION_STATUSES
from app.risk.models import ReservedExposure
from app.risk.sizing import ceil_to


def active_rows(session: Session, account_id: object) -> list[RiskReservationRecord]:
    """Active reservations of the account, oldest first (a stable, deterministic order)."""
    return list(
        session.scalars(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.portfolio_account_id == account_id,
                RiskReservationRecord.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .order_by(RiskReservationRecord.requested_at, RiskReservationRecord.id)
        )
    )


def reserved_exposures(
    session: Session,
    account_id: object,
    functional: str,
    at: datetime,
    accounting: AccountingSettings,
    sources: tuple[FxRateSource, ...],
) -> tuple[list[ReservedExposure], list[dict[str, str]]]:
    places = currency(functional).minor_units
    max_age = timedelta(hours=accounting.fx_rate_max_age_hours)
    out: list[ReservedExposure] = []
    issues: list[dict[str, str]] = []
    for row in active_rows(session, account_id):
        factor = ONE
        if row.quote_currency != functional:
            found = functional_rate(
                session, row.quote_currency, functional, at, max_age=max_age, sources=sources
            )
            if found.status is not PriceStatus.FRESH or found.quote is None:
                issues.append(
                    {
                        "kind": f"RATE_{found.status.value}",
                        "reservation_id": str(row.id),
                        "currency": row.quote_currency,
                        "detail": found.reason or "no fresh rate",
                    }
                )
            elif row.fx_rate is not None and found.quote.rate > row.fx_rate:
                factor = div(found.quote.rate, row.fx_rate)
        tags = classification_at(session, row.asset_id, at)

        def scaled(amount: Decimal, f: Decimal = factor) -> Decimal:
            if f == ONE:
                return amount
            with money_context():
                return ceil_to(amount * f, places)

        out.append(
            ReservedExposure(
                reservation_id=row.id,
                asset_id=row.asset_id,
                symbol=row.symbol,
                asset_class=row.asset_class,
                direction=row.direction,
                sector=tags.sector,
                cluster=tags.cluster,
                remaining_quantity=row.remaining_quantity,
                exposure=scaled(row.remaining_exposure),
                capital=scaled(row.remaining_capital),
                margin=scaled(row.remaining_margin),
                risk=scaled(row.remaining_risk),
            )
        )
    return out, issues


__all__ = ["active_rows", "reserved_exposures"]

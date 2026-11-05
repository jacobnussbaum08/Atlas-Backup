"""FX rates and instrument specifications: recording and point-in-time lookup.

FX rates (D-131): a rate row says 1 ``base`` = ``rate`` ``quote`` at ``observed_at``, with its
source and provenance. The functional rate of a currency at time ``t`` is the newest rate
observed at or before ``t`` for the pair in either direction (an inverse is rounded to 12
places), from the sources the account may use: PROVIDER and OPERATOR always, TEST_FIXTURE
only on a CONTROLLED_TEST account. CONVERSION rows document executed conversions and are
never used to value anything. A rate older than the configured maximum age is STALE: the
caller decides what that means (a posting refuses it; a valuation flags it).

Instrument specifications (D-132): quantity increment (lot size), minimum quantity and
multiplier of an asset, effective-dated and append-only. Without one, nothing about the
asset can be posted: Atlas never guesses a lot size.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.accounting.enums import FxRateSource, InstrumentSpecSource, PriceStatus
from app.accounting.money import (
    ONE,
    PRICE_SCALE,
    QUANTITY_SCALE,
    RATE_SCALE,
    MoneyError,
    currency,
    div,
    require_positive,
    require_scale,
    round_to,
)
from app.accounting.templates import InstrumentInfo, RateQuote, TemplateError
from app.core.clocks import ensure_utc
from app.database.models import Asset, AssetReferenceVersionRecord
from app.database.models.accounting import FxRateRecord, InstrumentSpecRecord

VALUATION_SOURCES = (FxRateSource.PROVIDER, FxRateSource.OPERATOR)
TEST_SOURCES = (*VALUATION_SOURCES, FxRateSource.TEST_FIXTURE)


class ReferenceDataError(TemplateError):
    """A rate or instrument specification is missing, stale, or contradicts a stored one."""


@dataclass(frozen=True)
class RateLookup:
    currency: str
    status: PriceStatus
    quote: RateQuote | None
    reason: str | None = None


def record_fx_rate(
    session: Session,
    *,
    base: str,
    quote: str,
    rate: Decimal,
    observed_at: datetime,
    source: FxRateSource,
    provenance: str,
    recorded_at: datetime,
    created_by: str,
    provider: str | None = None,
) -> FxRateRecord:
    """Store a rate; storing the same rate again is a no-op, a different one is refused."""
    base, quote = currency(base).code, currency(quote).code
    if base == quote:
        raise ReferenceDataError("a rate needs two different currencies")
    try:
        require_positive(require_scale(rate, RATE_SCALE, "rate"), "rate")
    except MoneyError as exc:
        raise ReferenceDataError(str(exc)) from None
    observed_at = ensure_utc(observed_at)
    if observed_at > recorded_at:
        raise ReferenceDataError("a rate cannot be observed after it is recorded")
    existing = session.scalar(
        select(FxRateRecord).where(
            FxRateRecord.base_currency == base,
            FxRateRecord.quote_currency == quote,
            FxRateRecord.source == source,
            FxRateRecord.observed_at == observed_at,
        )
    )
    if existing is not None:
        if existing.rate != rate:
            raise ReferenceDataError(
                f"a different {source.value} rate for {base}/{quote} at {observed_at} exists"
            )
        return existing
    row = FxRateRecord(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        observed_at=observed_at,
        source=source,
        provider=provider,
        provenance=provenance,
        recorded_at=recorded_at,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return row


def functional_rate(
    session: Session,
    cur: str,
    functional_currency: str,
    at: datetime,
    *,
    max_age: timedelta,
    sources: tuple[FxRateSource, ...] = VALUATION_SOURCES,
) -> RateLookup:
    """Functional-currency units per unit of ``cur`` known at ``at``."""
    if cur == functional_currency:
        raise ValueError("the functional currency has no rate")
    stmt = (
        select(FxRateRecord)
        .where(
            or_(
                and_(
                    FxRateRecord.base_currency == cur,
                    FxRateRecord.quote_currency == functional_currency,
                ),
                and_(
                    FxRateRecord.base_currency == functional_currency,
                    FxRateRecord.quote_currency == cur,
                ),
            ),
            FxRateRecord.source.in_(sources),
            FxRateRecord.observed_at <= at,
            FxRateRecord.recorded_at <= at,
        )
        .order_by(FxRateRecord.observed_at.desc(), FxRateRecord.id.desc())
        .limit(1)
    )
    row = session.scalar(stmt)
    if row is None:
        return RateLookup(cur, PriceStatus.MISSING, None, f"no {cur}/{functional_currency} rate")
    rate = row.rate if row.base_currency == cur else round_to(div(ONE, row.rate), RATE_SCALE)
    quote = RateQuote(cur, rate, row.id, row.observed_at, row.source.value)
    if at - row.observed_at > max_age:
        return RateLookup(
            cur, PriceStatus.STALE, quote, f"{cur} rate observed {row.observed_at.isoformat()}"
        )
    return RateLookup(cur, PriceStatus.FRESH, quote)


def register_instrument_spec(
    session: Session,
    *,
    asset_id: uuid.UUID,
    quantity_increment: Decimal,
    min_quantity: Decimal,
    valid_from: datetime,
    source: InstrumentSpecSource,
    provenance: str,
    created_by: str,
    price_increment: Decimal | None = None,
    multiplier: Decimal = ONE,
) -> InstrumentSpecRecord:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ReferenceDataError(f"asset {asset_id} does not exist")
    try:
        require_positive(
            require_scale(quantity_increment, QUANTITY_SCALE, "increment"), "increment"
        )
        require_scale(min_quantity, QUANTITY_SCALE, "minimum quantity")
        if price_increment is not None:
            require_positive(require_scale(price_increment, PRICE_SCALE, "tick"), "tick")
        currency(asset.quote_currency)
    except MoneyError as exc:
        raise ReferenceDataError(str(exc)) from None
    if min_quantity < 0:
        raise ReferenceDataError("the minimum quantity cannot be negative")
    valid_from = ensure_utc(valid_from)
    existing = session.scalar(
        select(InstrumentSpecRecord).where(
            InstrumentSpecRecord.asset_id == asset_id, InstrumentSpecRecord.valid_from == valid_from
        )
    )
    values = (quantity_increment, min_quantity, price_increment, multiplier)
    if existing is not None:
        stored = (
            existing.quantity_increment,
            existing.min_quantity,
            existing.price_increment,
            existing.multiplier,
        )
        if stored != values:
            raise ReferenceDataError(f"a different specification from {valid_from} exists")
        return existing
    row = InstrumentSpecRecord(
        asset_id=asset_id,
        currency=asset.quote_currency,
        quantity_increment=quantity_increment,
        min_quantity=min_quantity,
        price_increment=price_increment,
        multiplier=multiplier,
        valid_from=valid_from,
        source=source,
        provenance=provenance,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return row


def spec_from_reference(session: Session, asset_id: uuid.UUID, at: datetime) -> dict[str, object]:
    """Lot size from the newest provider reference version, or a refusal explaining why."""
    version = session.scalar(
        select(AssetReferenceVersionRecord)
        .where(
            AssetReferenceVersionRecord.asset_id == asset_id,
            AssetReferenceVersionRecord.observed_at <= at,
        )
        .order_by(AssetReferenceVersionRecord.observed_at.desc())
        .limit(1)
    )
    if version is None:
        raise ReferenceDataError("no provider reference data for the asset")
    if version.min_trade_increment is not None and version.min_trade_increment > 0:
        increment = version.min_trade_increment
        basis = "min_trade_increment"
    elif version.fractionable is False:
        increment = ONE
        basis = "not fractionable: whole units"
    else:
        raise ReferenceDataError(
            "the provider reports no trade increment for a fractionable asset; register the "
            "specification explicitly"
        )
    minimum = version.min_order_size if version.min_order_size is not None else increment
    return {
        "quantity_increment": increment,
        "min_quantity": minimum,
        "price_increment": version.price_increment,
        "provenance": (
            f"{version.provider} reference version {version.version} observed "
            f"{version.observed_at.isoformat()} ({basis})"
        ),
    }


def instrument_info(session: Session, asset_id: uuid.UUID, at: datetime) -> InstrumentInfo:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ReferenceDataError(f"asset {asset_id} does not exist")
    spec = session.scalar(
        select(InstrumentSpecRecord)
        .where(InstrumentSpecRecord.asset_id == asset_id, InstrumentSpecRecord.valid_from <= at)
        .order_by(InstrumentSpecRecord.valid_from.desc())
        .limit(1)
    )
    if spec is None:
        raise ReferenceDataError(
            f"no instrument specification for {asset.symbol} at {at.isoformat()}: register one "
            "(lot size is never guessed)"
        )
    if spec.currency != asset.quote_currency:
        raise ReferenceDataError(f"{asset.symbol}: specification currency differs from the asset's")
    return InstrumentInfo(
        asset_id=asset.id,
        symbol=asset.symbol,
        asset_class=asset.asset_class.value,
        currency=spec.currency,
        quantity_increment=spec.quantity_increment,
        min_quantity=spec.min_quantity,
        multiplier=spec.multiplier,
        spec_id=spec.id,
    )


__all__ = [
    "TEST_SOURCES",
    "VALUATION_SOURCES",
    "RateLookup",
    "ReferenceDataError",
    "functional_rate",
    "instrument_info",
    "record_fx_rate",
    "register_instrument_spec",
    "spec_from_reference",
]

"""Point-in-time inputs of the risk engine, read from stored data (Phase 6B).

Everything is read as Atlas knew it at the assessment time ``at``: a row counts only if it
was observed (and became available) at or before ``at``. Nothing is fetched from a provider,
interpolated or invented; a missing input is reported as missing.

* lot size: ``instrument_specs`` (Phase 6A; never guessed);
* tradability, shortability, borrow, short margin requirement, price increment: the newest
  provider reference version (``asset_reference_versions``, Phase 3A);
* sector and correlation cluster: ``risk_asset_classifications`` (operator tags);
* price and freshness: ``app.portfolio.pricing`` (the same rules as valuation checkpoints);
* spread: the newest VALID quote within ``quote_max_age_minutes``;
* liquidity: the newest OK ``median_dollar_volume`` measurement (Phase 3A metrics) no older
  than ``liquidity_max_age_days``;
* FX: ``fx_rates`` (Phase 6A rules and sources);
* correlation: the newest OK PAIR correlation (Phase 3B) no older than
  ``correlation_max_age_days``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.accounting.config import AccountingSettings
from app.accounting.enums import FxRateSource, PriceStatus
from app.accounting.money import CURRENCIES
from app.accounting.reference import ReferenceDataError, functional_rate, instrument_info
from app.asset_discovery.enums import MetricStatus
from app.data_engine.enums import DataQualityStatus
from app.database.models import (
    Asset,
    AssetMetricValueRecord,
    AssetReferenceVersionRecord,
    CrossAssetRelationshipRecord,
    MarketQuoteRecord,
)
from app.database.models.risk import RiskAssetClassificationRecord
from app.market_intelligence.enums import RelationKind
from app.portfolio.pricing import valuation_price
from app.risk.config import Preferences
from app.risk.models import Correlation, InstrumentData, MarketData

LIQUIDITY_METRIC = "median_dollar_volume"
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Classification:
    sector: str | None
    cluster: str | None
    record_id: int | None


def classification_at(session: Session, asset_id: uuid.UUID, at: datetime) -> Classification:
    row = session.scalar(
        select(RiskAssetClassificationRecord)
        .where(
            RiskAssetClassificationRecord.asset_id == asset_id,
            RiskAssetClassificationRecord.valid_from <= at,
        )
        .order_by(RiskAssetClassificationRecord.valid_from.desc())
        .limit(1)
    )
    if row is None:
        return Classification(None, None, None)
    return Classification(row.sector, row.cluster, row.id)


def reference_at(
    session: Session, asset_id: uuid.UUID, at: datetime
) -> AssetReferenceVersionRecord | None:
    v = AssetReferenceVersionRecord
    return session.scalar(
        select(v)
        .where(v.asset_id == asset_id, v.observed_at <= at)
        .order_by(v.observed_at.desc(), v.version.desc())
        .limit(1)
    )


def short_margin_fraction(reference: AssetReferenceVersionRecord | None) -> Decimal | None:
    """The provider's short margin requirement as a fraction. Providers report a percentage
    (``100`` = 100%); Atlas reads it as collateral required beyond the restricted proceeds,
    the conservative reading (decision D-145)."""
    if reference is None or reference.margin_requirement_short is None:
        return None
    if reference.margin_requirement_short < 0:
        return None
    return reference.margin_requirement_short / HUNDRED


def instrument_data(session: Session, asset: Asset, at: datetime) -> InstrumentData:
    increment = minimum = spec_id = None
    multiplier = Decimal(1)
    try:
        info = instrument_info(session, asset.id, at)
        increment, minimum, multiplier, spec_id = (
            info.quantity_increment,
            info.min_quantity,
            info.multiplier,
            info.spec_id,
        )
    except ReferenceDataError:
        pass
    reference = reference_at(session, asset.id, at)
    tags = classification_at(session, asset.id, at)
    return InstrumentData(
        asset_id=asset.id,
        symbol=asset.symbol,
        asset_class=asset.asset_class.value,
        asset_status=asset.status.value,
        quote_currency=asset.quote_currency,
        currency_supported=asset.quote_currency in CURRENCIES,
        quantity_increment=increment,
        min_quantity=minimum,
        multiplier=multiplier,
        spec_id=spec_id,
        provider_tick_size=None if reference is None else reference.price_increment,
        reference_known=reference is not None,
        tradable=None if reference is None else reference.tradable,
        shortable=None if reference is None else reference.shortable,
        easy_to_borrow=None if reference is None else reference.easy_to_borrow,
        margin_requirement_short=short_margin_fraction(reference),
        reference_version=None if reference is None else reference.version,
        reference_observed_at=None if reference is None else reference.observed_at,
        sector=tags.sector,
        cluster=tags.cluster,
        classification_id=tags.record_id,
    )


def _latest_quote(session: Session, asset_id: uuid.UUID, at: datetime) -> MarketQuoteRecord | None:
    q = MarketQuoteRecord
    return session.scalar(
        select(q)
        .where(
            q.asset_id == asset_id,
            q.event_time <= at,
            q.available_at <= at,
            q.quality_status == DataQualityStatus.VALID,
            q.bid_price > 0,
            q.ask_price >= q.bid_price,
        )
        .order_by(q.event_time.desc(), q.id.desc())
        .limit(1)
    )


def _liquidity(
    session: Session, asset_id: uuid.UUID, at: datetime, max_age: timedelta
) -> AssetMetricValueRecord | None:
    m = AssetMetricValueRecord
    return session.scalar(
        select(m)
        .where(
            m.asset_id == asset_id,
            m.metric == LIQUIDITY_METRIC,
            m.status == MetricStatus.OK,
            m.as_of <= at,
            m.as_of >= at - max_age,
        )
        .order_by(m.as_of.desc(), m.id.desc())
        .limit(1)
    )


def market_data(
    session: Session,
    asset: Asset,
    at: datetime,
    *,
    functional_currency: str,
    accounting: AccountingSettings,
    preferences: Preferences,
    rate_sources: tuple[FxRateSource, ...],
) -> MarketData:
    price = valuation_price(session, asset, at, accounting)
    obs = price.observation
    quote = _latest_quote(session, asset.id, at)
    quote_fresh = quote is not None and at - quote.event_time <= timedelta(
        minutes=preferences.quote_max_age_minutes
    )
    adv = _liquidity(session, asset.id, at, timedelta(days=preferences.liquidity_max_age_days))
    fx_rate: Decimal | None = Decimal(1)
    fx_status = "FRESH"
    fx_id: int | None = None
    fx_reason: str | None = None
    if asset.quote_currency != functional_currency:
        if asset.quote_currency not in CURRENCIES:
            fx_rate, fx_status, fx_reason = None, "MISSING", "not a ledger currency"
        else:
            found = functional_rate(
                session,
                asset.quote_currency,
                functional_currency,
                at,
                max_age=timedelta(hours=accounting.fx_rate_max_age_hours),
                sources=rate_sources,
            )
            fx_status = found.status.value
            fx_reason = found.reason
            fx_rate = found.quote.rate if found.quote is not None else None
            fx_id = found.quote.fx_rate_id if found.quote is not None else None
            if found.status is not PriceStatus.FRESH:
                fx_rate = fx_rate if found.quote is not None else None
    return MarketData(
        price=None if obs is None else obs.price,
        price_status=price.status.value,
        price_source=None if obs is None else obs.source,
        price_observed_at=None if obs is None else obs.observed_at,
        price_reason=price.reason,
        price_provenance={} if obs is None else obs.provenance,
        bid=None if quote is None else quote.bid_price,
        ask=None if quote is None else quote.ask_price,
        quote_observed_at=None if quote is None else quote.event_time,
        quote_fresh=quote_fresh,
        adv_quote=None if adv is None else adv.value,
        adv_metric=None if adv is None else f"{adv.metric} ({adv.window})",
        adv_as_of=None if adv is None else adv.as_of,
        fx_rate=fx_rate,
        fx_status=fx_status,
        fx_rate_id=fx_id,
        fx_reason=fx_reason,
    )


def correlations(
    session: Session,
    candidate: uuid.UUID,
    held: list[uuid.UUID],
    at: datetime,
    max_age: timedelta,
) -> list[Correlation]:
    """The newest usable |correlation| between the candidate and each held asset."""
    r = CrossAssetRelationshipRecord
    out: list[Correlation] = []
    for other in held:
        if other == candidate:
            continue
        row = session.scalar(
            select(r)
            .where(
                r.kind == RelationKind.PAIR,
                r.status == "OK",
                r.correlation.is_not(None),
                or_(
                    and_(r.asset_a == candidate, r.asset_b == other),
                    and_(r.asset_a == other, r.asset_b == candidate),
                ),
                r.as_of <= at,
                r.available_at <= at,
                r.as_of >= at - max_age,
            )
            .order_by(r.as_of.desc(), r.id.desc())
            .limit(1)
        )
        if row is None or row.correlation is None:
            out.append(Correlation(asset_id=other))
        else:
            out.append(
                Correlation(
                    asset_id=other,
                    value=abs(row.correlation),
                    as_of=row.as_of,
                    source=f"cross_asset_relationships:{row.id} ({row.algorithm}, {row.window})",
                )
            )
    return out


__all__ = [
    "LIQUIDITY_METRIC",
    "Classification",
    "classification_at",
    "correlations",
    "instrument_data",
    "market_data",
    "reference_at",
    "short_margin_fraction",
]

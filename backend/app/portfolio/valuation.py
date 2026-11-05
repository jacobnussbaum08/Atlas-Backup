"""Valuation checkpoints and portfolio snapshots (spec 13, 16.6; decision D-133).

A checkpoint at ``as_of``, in one transaction:

1. lock the account; read its positions and foreign-currency balances;
2. find, for each open position, the valuation price (``pricing``) and, for a foreign
   currency, the FX rate known at ``as_of``; only FRESH inputs are used;
3. post one VALUATION entry re-targeting the valuation adjustments of the positions with
   fresh inputs and the FX translation of the currencies with fresh rates (nothing is posted
   when nothing changed);
4. write an immutable snapshot of the resulting ledger figures and every position, recording
   the price and rate used with provenance, or why none was usable. A position or currency
   without a fresh input keeps its last posted valuation and the snapshot is flagged
   STALE or INCOMPLETE; no price is ever substituted.

A checkpoint is idempotent per (account, as_of): repeating it returns the stored snapshot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting import chart
from app.accounting.config import AccountingSettings
from app.accounting.enums import EntrySource, PriceStatus, ValuationStatus
from app.accounting.money import PRICE_SCALE, ZERO, div, round_to
from app.accounting.posting import (
    PostingError,
    PostingRequest,
    PostingService,
    last_sequence,
    load_state,
)
from app.accounting.reference import (
    TEST_SOURCES,
    VALUATION_SOURCES,
    RateLookup,
    functional_rate,
    instrument_info,
)
from app.accounting.templates import PriceMark, TemplateError, ValuationParams
from app.core.clocks import Clock, ensure_utc
from app.core.logging import get_logger
from app.database.models import Asset, PortfolioAccount
from app.database.models.portfolio import PortfolioSnapshotPositionRecord, PortfolioSnapshotRecord
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.database.sessions import session_scope
from app.portfolio.figures import cash_by_currency, figures
from app.portfolio.pricing import PriceResult, valuation_price

logger = get_logger(__name__)


@dataclass(frozen=True)
class CheckpointResult:
    snapshot_id: uuid.UUID
    as_of: datetime
    valuation_status: ValuationStatus
    valuation_entry_id: uuid.UUID | None
    equity: Decimal
    issues: list[dict[str, Any]]
    created: bool


class ValuationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        settings: AccountingSettings,
        posting: PostingService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings
        self._posting = posting

    def checkpoint(
        self, account_id: uuid.UUID, *, as_of: datetime | None = None, actor: str
    ) -> CheckpointResult:
        with session_scope(self._session_factory) as session:
            return self.checkpoint_in(session, account_id, as_of=as_of, actor=actor)

    def checkpoint_in(
        self,
        session: Session,
        account_id: uuid.UUID,
        *,
        as_of: datetime | None = None,
        actor: str,
    ) -> CheckpointResult:
        now = self._clock.now()
        as_of = ensure_utc(as_of) if as_of is not None else now
        if as_of > now:
            raise PostingError("a valuation cannot be taken in the future")
        account = session.scalar(
            select(PortfolioAccount).where(PortfolioAccount.id == account_id).with_for_update()
        )
        if account is None:
            raise PostingError(f"portfolio account {account_id} does not exist")
        existing = session.scalar(
            select(PortfolioSnapshotRecord).where(
                PortfolioSnapshotRecord.portfolio_account_id == account_id,
                PortfolioSnapshotRecord.as_of == as_of,
            )
        )
        if existing is not None:
            return CheckpointResult(
                existing.id,
                existing.as_of,
                existing.valuation_status,
                existing.valuation_entry_id,
                existing.equity,
                existing.issues,
                created=False,
            )
        latest = session.scalar(
            select(PortfolioSnapshotRecord.as_of)
            .where(PortfolioSnapshotRecord.portfolio_account_id == account_id)
            .order_by(PortfolioSnapshotRecord.as_of.desc())
            .limit(1)
        )
        if latest is not None and as_of < latest:
            raise PostingError(f"a later snapshot ({latest.isoformat()}) already exists")

        functional = account.base_currency
        sources = (
            TEST_SOURCES
            if account.purpose is PortfolioAccountPurpose.CONTROLLED_TEST
            else VALUATION_SOURCES
        )
        state = load_state(session, account_id, functional)
        rates: dict[str, RateLookup] = {}

        def rate(cur: str) -> RateLookup:
            if cur not in rates:
                rates[cur] = functional_rate(
                    session,
                    cur,
                    functional,
                    as_of,
                    max_age=timedelta(hours=self._settings.fx_rate_max_age_hours),
                    sources=sources,
                )
            return rates[cur]

        issues: list[dict[str, Any]] = []
        marks: list[PriceMark] = []
        observed: dict[uuid.UUID, tuple[Asset, PriceResult, RateLookup | None]] = {}
        for asset_id, pos in sorted(state.positions.items(), key=lambda item: str(item[0])):
            if pos.quantity == ZERO:
                continue
            asset = session.get(Asset, asset_id)
            if asset is None:  # pragma: no cover - foreign keys prevent it
                raise PostingError(f"asset {asset_id} disappeared")
            price = valuation_price(session, asset, as_of, self._settings)
            fx = rate(pos.currency) if pos.currency != functional else None
            observed[asset_id] = (asset, price, fx)
            if price.status is not PriceStatus.FRESH or price.observation is None:
                issues.append(
                    {
                        "kind": f"PRICE_{price.status.value}",
                        "asset_id": str(asset_id),
                        "symbol": asset.symbol,
                        "detail": price.reason,
                        "observed_at": price.observation.observed_at.isoformat()
                        if price.observation
                        else None,
                    }
                )
                continue
            if fx is not None and fx.status is not PriceStatus.FRESH:
                issues.append(
                    {
                        "kind": f"RATE_{fx.status.value}",
                        "asset_id": str(asset_id),
                        "symbol": asset.symbol,
                        "currency": pos.currency,
                        "detail": fx.reason,
                    }
                )
                continue
            try:
                multiplier = instrument_info(session, asset_id, as_of).multiplier
            except TemplateError as exc:
                issues.append(
                    {
                        "kind": "SPEC_MISSING",
                        "asset_id": str(asset_id),
                        "symbol": asset.symbol,
                        "detail": str(exc),
                    }
                )
                continue
            marks.append(
                PriceMark(
                    asset_id=asset_id,
                    price=price.observation.price,
                    currency=pos.currency,
                    multiplier=multiplier,
                    provenance=price.observation.provenance,
                )
            )

        translate: list[str] = []
        for cur in sorted(state.currencies()):
            if cur == functional:
                continue
            net, _ = state.net_position(cur)
            if net == ZERO and state.translation(cur) == ZERO:
                continue
            found = rate(cur)
            if found.status is PriceStatus.FRESH:
                translate.append(cur)
            else:
                issues.append(
                    {"kind": f"RATE_{found.status.value}", "currency": cur, "detail": found.reason}
                )

        entry_id: uuid.UUID | None = None
        if marks or translate:
            posted = self._posting.post_or_skip_in(
                session,
                PostingRequest(
                    account_id=account_id,
                    template="valuation",
                    params=ValuationParams(
                        as_of=as_of, marks=tuple(marks), translate=tuple(translate)
                    ),
                    event_id=f"valuation:{as_of.isoformat()}",
                    effective_at=as_of,
                    source=EntrySource.SYSTEM,
                    actor=actor,
                ),
                skip_if_empty=True,
            )
            entry_id = posted.entry_id if posted is not None else None

        after = load_state(session, account_id, functional)
        totals = figures(after)
        if totals.equity != totals.equity_by_components:
            raise PostingError(
                f"accounting equation broken: assets - liabilities {totals.equity} != "
                f"components {totals.equity_by_components}; snapshot not written"
            )
        if any(i["kind"].endswith("MISSING") for i in issues):
            status = ValuationStatus.INCOMPLETE
        elif issues:
            status = ValuationStatus.STALE
        else:
            status = ValuationStatus.COMPLETE
        sequence = last_sequence(session, account_id)
        snapshot = PortfolioSnapshotRecord(
            portfolio_account_id=account_id,
            as_of=as_of,
            entry_sequence=sequence,
            valuation_entry_id=entry_id,
            valuation_status=status,
            functional_currency=functional,
            cash_by_currency=cash_by_currency(after),
            issues=issues,
            created_by=actor,
            **{
                name: getattr(totals, name)
                for name in (
                    "contributed_capital",
                    "cash",
                    "restricted_cash",
                    "long_value",
                    "short_liability",
                    "accrued_costs",
                    "fx_translation",
                    "total_assets",
                    "total_liabilities",
                    "equity",
                    "realized_pnl",
                    "unrealized_pnl",
                    "realized_fx",
                    "unrealized_fx",
                    "commissions",
                    "fees",
                    "borrow_costs",
                    "funding_costs",
                    "investment_return",
                    "available_capital",
                    "long_exposure",
                    "short_exposure",
                    "gross_exposure",
                    "net_exposure",
                )
            },
        )
        session.add(snapshot)
        session.flush()
        marked = {m.asset_id for m in marks}
        for asset_id, (asset, price, fx) in observed.items():
            pos = after.positions[asset_id]
            if pos.is_long:
                adj = after.balance(chart.LONG_VALUATION, pos.currency, asset_id)
                carried = pos.cost + adj.amount
                adjustment = adj.amount
                carried_functional = (
                    after.balance(chart.LONG_COST, pos.currency, asset_id).functional
                    + adj.functional
                )
            else:
                adj = after.balance(chart.SHORT_VALUATION, pos.currency, asset_id)
                carried = pos.cost - adj.amount
                adjustment = -adj.amount
                carried_functional = -(
                    after.balance(chart.SHORT_PROCEEDS, pos.currency, asset_id).functional
                    + adj.functional
                )
            obs = price.observation
            session.add(
                PortfolioSnapshotPositionRecord(
                    snapshot_id=snapshot.id,
                    asset_id=asset_id,
                    symbol=asset.symbol,
                    asset_class=asset.asset_class.value,
                    currency=pos.currency,
                    quantity=pos.quantity,
                    cost_basis=pos.cost,
                    average_price=round_to(div(pos.cost, abs(pos.quantity)), PRICE_SCALE),
                    price=obs.price if obs is not None else None,
                    price_status=price.status,
                    price_source=obs.source if obs is not None else None,
                    price_observed_at=obs.observed_at if obs is not None else None,
                    price_provenance=obs.provenance if obs is not None else {},
                    marked=asset_id in marked,
                    carried_value=carried,
                    valuation_adjustment=adjustment,
                    carried_value_functional=carried_functional,
                    fx_rate=fx.quote.rate if fx is not None and fx.quote is not None else None,
                    fx_rate_id=fx.quote.fx_rate_id if fx is not None and fx.quote else None,
                )
            )
        session.flush()
        logger.info(
            "portfolio.checkpoint",
            account_id=str(account_id),
            as_of=as_of.isoformat(),
            status=status.value,
            issues=len(issues),
            posted=entry_id is not None,
        )
        return CheckpointResult(
            snapshot.id, as_of, status, entry_id, totals.equity, issues, created=True
        )


__all__ = ["CheckpointResult", "ValuationService"]

"""TEST FIXTURE builders for pure risk-assessment inputs (Phase 6B).

Every value here is invented for deterministic test vectors: no market data, no provider
response. ``ZERO_COSTS`` removes every cost assumption so the spec formula can be checked
exactly; the default preferences exercise the cost model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

from app.risk.config import HardLimits, Preferences, RiskConfiguration
from app.risk.enums import BreakerType, MarkStatus, ProposalSource, TradeDirection
from app.risk.models import (
    AssessmentInputs,
    Correlation,
    HeldPosition,
    InstrumentData,
    MarketData,
    PortfolioView,
    Proposal,
)

AT = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
ASSET = uuid.UUID("00000000-0000-0000-0000-00000000a001")
ACCOUNT = uuid.UUID("00000000-0000-0000-0000-00000000c001")
ZERO_COSTS = Preferences(
    commission_per_unit="0",
    min_commission="0",
    fee_bps="0",
    slippage_bps="0",
    stop_slippage_bps="0",
    fallback_spread_bps="0",
    fx_conversion_cost_bps="0",
    short_borrow_rate_annual="0",
)


def config(
    limits: dict[str, Any] | None = None, prefs: Preferences | dict[str, Any] | None = None
) -> RiskConfiguration:
    if isinstance(prefs, dict):
        preferences = Preferences.model_validate({**ZERO_COSTS.canonical(), **prefs})
    else:
        preferences = prefs if prefs is not None else ZERO_COSTS
    return RiskConfiguration(
        version_id=str(uuid.UUID(int=1)),
        version=1,
        hard_limits=HardLimits.model_validate({**HardLimits().canonical(), **(limits or {})}),
        preferences=preferences,
    )


def proposal(
    direction: TradeDirection = TradeDirection.LONG,
    entry: str = "100",
    stop: str | None = "95",
    **extra: Any,
) -> Proposal:
    return Proposal(
        source=ProposalSource.OPERATOR,
        asset_id=extra.pop("asset_id", ASSET),
        direction=direction,
        entry=D(entry),
        stop=None if stop is None else D(stop),
        **extra,
    )


def instrument(**overrides: Any) -> InstrumentData:
    values: dict[str, Any] = {
        "asset_id": ASSET,
        "symbol": "FIXT",
        "asset_class": "EQUITY",
        "asset_status": "ACTIVE",
        "quote_currency": "USD",
        "currency_supported": True,
        "quantity_increment": D(1),
        "min_quantity": D(1),
        "reference_known": True,
        "tradable": True,
        "shortable": True,
        "easy_to_borrow": True,
        "margin_requirement_short": D("1"),
        "reference_version": 1,
    }
    values.update(overrides)
    return InstrumentData(**values)


def market(price: str | None = "100", **overrides: Any) -> MarketData:
    values: dict[str, Any] = {
        "price": None if price is None else D(price),
        "price_status": "FRESH",
        "price_source": "BAR_CLOSE",
        "bid": None if price is None else D(price),
        "ask": None if price is None else D(price),
        "quote_fresh": True,
        "adv_quote": D("50000000"),
        "fx_rate": D(1),
        "fx_status": "FRESH",
    }
    values.update(overrides)
    return MarketData(**values)


def held(
    symbol: str,
    exposure: str,
    *,
    direction: TradeDirection = TradeDirection.LONG,
    asset_id: uuid.UUID | None = None,
    asset_class: str = "EQUITY",
    sector: str | None = None,
    cluster: str | None = None,
    margin: str = "0",
) -> HeldPosition:
    return HeldPosition(
        asset_id=asset_id or uuid.uuid5(uuid.NAMESPACE_DNS, symbol),
        symbol=symbol,
        asset_class=asset_class,
        currency="USD",
        direction=direction,
        quantity=D(1) if direction is TradeDirection.LONG else D(-1),
        exposure=D(exposure),
        price_status="FRESH",
        margin_required=D(margin),
        sector=sector,
        cluster=cluster,
    )


def portfolio(
    equity: str = "100000",
    positions: list[HeldPosition] | None = None,
    **overrides: Any,
) -> PortfolioView:
    positions = positions or []
    long_ = sum((p.exposure for p in positions if p.direction is TradeDirection.LONG), D(0))
    short = sum((p.exposure for p in positions if p.direction is TradeDirection.SHORT), D(0))
    values: dict[str, Any] = {
        "account_id": ACCOUNT,
        "functional_currency": "USD",
        "ledger_sequence": 1,
        "mark_status": MarkStatus.COMPLETE,
        "equity": D(equity),
        "contributed_capital": D("100000"),
        "cash": D(equity) - long_,
        "available_capital": D(equity) - long_,
        "long_exposure": long_,
        "short_exposure": short,
        "gross_exposure": long_ + short,
        "net_exposure": long_ - short,
        "margin_used": sum((p.margin_required for p in positions), D(0)),
        "positions": positions,
        "daily_loss": D(0),
        "daily_baseline": D(equity),
        "weekly_loss": D(0),
        "weekly_baseline": D(equity),
        "peak_equity": D(equity),
        "drawdown": D(0),
        "available_risk_budget": D(equity) * D("0.02"),
        "reconciliation_status": "PASSED",
        "reconciliation_current": True,
    }
    values.update(overrides)
    return PortfolioView(**values)


def inputs(
    *,
    prop: Proposal | None = None,
    ins: InstrumentData | None = None,
    mkt: MarketData | None = None,
    port: PortfolioView | None = None,
    cfg: RiskConfiguration | None = None,
    correlations: list[Correlation] | None = None,
    breakers: list[BreakerType] | None = None,
    no_config: bool = False,
) -> AssessmentInputs:
    port = port or portfolio()
    if breakers:
        port = port.model_copy(update={"active_breakers": breakers})
    return AssessmentInputs(
        assessed_at=AT,
        proposal=prop or proposal(),
        instrument=ins or instrument(),
        market=mkt or market(),
        portfolio=port,
        correlations=correlations or [],
        config=None if no_config else (cfg or config()),
    )

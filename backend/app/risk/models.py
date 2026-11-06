"""Inputs and outputs of the pure risk assessment (Phase 6B).

Everything the assessment decides from is in :class:`AssessmentInputs`; it is stored with
every assessment as JSON (Decimals as strings), so a stored decision can be replayed and must
reproduce the same result (decision D-138). Nothing here reads a database or a clock.

Values are in the functional (reporting) currency unless a field says ``quote``: those are in
the instrument's quote currency.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.risk.config import RiskConfiguration, StrictDecimal
from app.risk.enums import (
    AssessmentResult,
    BreakerType,
    CheckOutcome,
    MarkStatus,
    ProposalSource,
    TradeDirection,
)


class ReasonCode(StrEnum):
    # State and governance
    BREAKER_ACTIVE = "BREAKER_ACTIVE"
    NO_CONFIGURATION = "NO_CONFIGURATION"
    PORTFOLIO_NOT_MARKED = "PORTFOLIO_NOT_MARKED"
    EQUITY_NOT_POSITIVE = "EQUITY_NOT_POSITIVE"
    RECONCILIATION_MISSING = "RECONCILIATION_MISSING"
    RECONCILIATION_NOT_CURRENT = "RECONCILIATION_NOT_CURRENT"
    LOSS_STATE_UNKNOWN = "LOSS_STATE_UNKNOWN"
    # The proposal itself
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    INVALID_ENTRY = "INVALID_ENTRY"
    MISSING_STOP = "MISSING_STOP"
    STOP_WRONG_SIDE = "STOP_WRONG_SIDE"
    STOP_TOO_CLOSE = "STOP_TOO_CLOSE"
    INVALID_REQUESTED_QUANTITY = "INVALID_REQUESTED_QUANTITY"
    REQUESTED_QUANTITY_REDUCED = "REQUESTED_QUANTITY_REDUCED"
    # Instrument
    INSTRUMENT_NOT_SUPPORTED = "INSTRUMENT_NOT_SUPPORTED"
    ASSET_CLASS_NOT_ENABLED = "ASSET_CLASS_NOT_ENABLED"
    INSTRUMENT_NOT_TRADABLE = "INSTRUMENT_NOT_TRADABLE"
    TRADABILITY_UNKNOWN = "TRADABILITY_UNKNOWN"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    LOT_SIZE_MISSING = "LOT_SIZE_MISSING"
    TICK_SIZE_MISSING = "TICK_SIZE_MISSING"
    # Market data
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_STALE = "PRICE_STALE"
    ENTRY_DEVIATES_FROM_MARKET = "ENTRY_DEVIATES_FROM_MARKET"
    STOP_ALREADY_BREACHED = "STOP_ALREADY_BREACHED"
    FX_RATE_MISSING = "FX_RATE_MISSING"
    FX_RATE_STALE = "FX_RATE_STALE"
    LIQUIDITY_DATA_MISSING = "LIQUIDITY_DATA_MISSING"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SPREAD_ASSUMED = "SPREAD_ASSUMED"
    # Short selling
    SHORTS_DISABLED = "SHORTS_DISABLED"
    SHORT_NOT_SUPPORTED = "SHORT_NOT_SUPPORTED"
    SHORTABILITY_UNKNOWN = "SHORTABILITY_UNKNOWN"
    NOT_SHORTABLE = "NOT_SHORTABLE"
    BORROW_UNAVAILABLE = "BORROW_UNAVAILABLE"
    BORROW_COST_ASSUMED = "BORROW_COST_ASSUMED"
    MARGIN_DATA_MISSING = "MARGIN_DATA_MISSING"
    MARGIN_RATE_ASSUMED = "MARGIN_RATE_ASSUMED"
    # Portfolio limits
    OPPOSITE_POSITION_HELD = "OPPOSITE_POSITION_HELD"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    RISK_BUDGET_EXHAUSTED = "RISK_BUDGET_EXHAUSTED"
    RISK_BUDGET = "RISK_BUDGET"
    COSTS_EXCEED_BUDGET = "COSTS_EXCEED_BUDGET"
    AVAILABLE_CAPITAL = "AVAILABLE_CAPITAL"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    LEVERAGE = "LEVERAGE"
    NET_EXPOSURE = "NET_EXPOSURE"
    SINGLE_ASSET_EXPOSURE = "SINGLE_ASSET_EXPOSURE"
    ASSET_CLASS_EXPOSURE = "ASSET_CLASS_EXPOSURE"
    SECTOR_EXPOSURE = "SECTOR_EXPOSURE"
    SECTOR_UNCLASSIFIED = "SECTOR_UNCLASSIFIED"
    CORRELATED_EXPOSURE = "CORRELATED_EXPOSURE"
    SHORT_EXPOSURE = "SHORT_EXPOSURE"
    MARGIN_USAGE = "MARGIN_USAGE"
    LIQUIDITY = "LIQUIDITY"
    BELOW_MINIMUM_QUANTITY = "BELOW_MINIMUM_QUANTITY"
    # Informational
    COST_ASSUMPTIONS = "COST_ASSUMPTIONS"
    STOP_ASSUMED = "STOP_ASSUMED"
    PENDING_EXPOSURE = "PENDING_EXPOSURE"
    # Phase 6C reservations
    RESERVED_CAPACITY = "RESERVED_CAPACITY"
    OPPOSITE_RESERVATION_ACTIVE = "OPPOSITE_RESERVATION_ACTIVE"
    RESERVATION_VALUATION_UNKNOWN = "RESERVATION_VALUATION_UNKNOWN"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Proposal(_Frozen):
    """A trade proposal as the risk engine sees it: levels and provenance, nothing else.

    Scores, probabilities, rankings and explanations of strategies or AI decisions are
    deliberately absent: they cannot influence a risk decision (decision D-140).
    """

    source: ProposalSource
    asset_id: uuid.UUID
    direction: TradeDirection
    entry: StrictDecimal
    stop: StrictDecimal | None = None
    target: StrictDecimal | None = None
    requested_quantity: StrictDecimal | None = None
    expected_holding_days: int | None = Field(default=None, ge=1, le=365)
    expires_at: datetime | None = None
    strategy_proposal_id: uuid.UUID | None = None
    ai_decision_id: uuid.UUID | None = None


class InstrumentData(_Frozen):
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    asset_status: str
    quote_currency: str
    currency_supported: bool
    # Lot size from ``instrument_specs`` (never guessed, D-132).
    quantity_increment: StrictDecimal | None = None
    min_quantity: StrictDecimal | None = None
    multiplier: StrictDecimal = Decimal(1)
    spec_id: int | None = None
    # Price increment reported by the provider (None when not reported).
    provider_tick_size: StrictDecimal | None = None
    # Point-in-time provider reference flags (Phase 3A versions); None = not reported.
    reference_known: bool = False
    tradable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    # Provider short margin requirement as a fraction (100% -> 1); None = not reported.
    margin_requirement_short: StrictDecimal | None = None
    reference_version: int | None = None
    reference_observed_at: datetime | None = None
    # Operator classification (``risk_asset_classifications``).
    sector: str | None = None
    cluster: str | None = None
    classification_id: int | None = None


class MarketData(_Frozen):
    price: StrictDecimal | None = None
    price_status: str = "MISSING"  # FRESH / STALE / MISSING (portfolio.pricing rules)
    price_source: str | None = None
    price_observed_at: datetime | None = None
    price_reason: str | None = None
    price_provenance: dict[str, Any] = Field(default_factory=dict)
    bid: StrictDecimal | None = None
    ask: StrictDecimal | None = None
    quote_observed_at: datetime | None = None
    quote_fresh: bool = False
    # Liquidity: median daily dollar volume (quote currency) and when it was measured.
    adv_quote: StrictDecimal | None = None
    adv_metric: str | None = None
    adv_as_of: datetime | None = None
    # Functional-currency units per quote-currency unit (1 for the functional currency).
    fx_rate: StrictDecimal | None = None
    fx_status: str = "MISSING"
    fx_rate_id: int | None = None
    fx_reason: str | None = None


class HeldPosition(_Frozen):
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    currency: str
    direction: TradeDirection
    quantity: StrictDecimal
    # Marked exposure (positive) in the functional currency.
    exposure: StrictDecimal
    price_status: str
    margin_required: StrictDecimal = Decimal(0)
    sector: str | None = None
    cluster: str | None = None


class ReservedExposure(_Frozen):
    """The unconsumed remainder of an active risk reservation (Phase 6C), functional currency.

    It counts against every limit exactly like a position would once it fills, and stops
    counting as it is consumed (the fill is then in the ledger) or released. Amounts are the
    approval-time amounts scaled to the remaining quantity, re-converted at the current rate
    when that is higher (never lower) for a foreign instrument.
    """

    reservation_id: uuid.UUID
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    direction: TradeDirection
    sector: str | None = None
    cluster: str | None = None
    remaining_quantity: StrictDecimal
    # Notional at the adverse entry fill (gross exposure).
    exposure: StrictDecimal
    # Available capital it commits (a long: notional and entry costs; a short: collateral
    # beyond its restricted proceeds and entry costs).
    capital: StrictDecimal
    margin: StrictDecimal = Decimal(0)
    # Potential loss at the stop, costs included.
    risk: StrictDecimal


class Correlation(_Frozen):
    """|correlation| of a held asset with the candidate, or None when not measured."""

    asset_id: uuid.UUID
    value: StrictDecimal | None = None
    as_of: datetime | None = None
    source: str | None = None


class PortfolioView(_Frozen):
    """The account as the risk engine sees it at the assessment time (marked to fresh prices)."""

    account_id: uuid.UUID
    functional_currency: str
    ledger_sequence: int
    mark_status: MarkStatus
    mark_issues: list[dict[str, Any]] = Field(default_factory=list)
    equity: StrictDecimal
    contributed_capital: StrictDecimal
    cash: StrictDecimal
    # Unrestricted cash - accrued unpaid costs (6A) - collateral committed to shorts.
    available_capital: StrictDecimal
    long_exposure: StrictDecimal
    short_exposure: StrictDecimal
    gross_exposure: StrictDecimal
    net_exposure: StrictDecimal
    margin_used: StrictDecimal
    positions: list[HeldPosition] = Field(default_factory=list)
    # Loss state (None when it could not be measured).
    daily_loss: StrictDecimal | None = None
    daily_baseline: StrictDecimal | None = None
    weekly_loss: StrictDecimal | None = None
    weekly_baseline: StrictDecimal | None = None
    peak_equity: StrictDecimal | None = None
    drawdown: StrictDecimal | None = None
    available_risk_budget: StrictDecimal | None = None
    # Exposure already committed but not yet in the ledger: the gross exposure of the active
    # risk reservations (Phase 6C), each listed in ``reservations``.
    pending_exposure: StrictDecimal = Decimal(0)
    pending_source: str = "none: no active risk reservation"
    reservations: list[ReservedExposure] = Field(default_factory=list)
    # Reservations that could not be valued (no fresh rate for a foreign one): no approval.
    reservation_issues: list[dict[str, Any]] = Field(default_factory=list)
    active_breakers: list[BreakerType] = Field(default_factory=list)
    reconciliation_status: str | None = None
    reconciliation_current: bool = False


class AssessmentInputs(_Frozen):
    assessed_at: datetime
    proposal: Proposal
    instrument: InstrumentData | None
    market: MarketData
    portfolio: PortfolioView
    correlations: list[Correlation] = Field(default_factory=list)
    config: RiskConfiguration | None


class Check(_Frozen):
    code: str
    outcome: CheckOutcome
    reason_code: ReasonCode | None = None
    message: str
    measured: StrictDecimal | None = None
    limit: StrictDecimal | None = None
    unit: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AssessmentOutcome(_Frozen):
    result: AssessmentResult
    checks: list[Check]
    permitted_quantity: StrictDecimal | None = None
    risk_budget: StrictDecimal | None = None
    potential_loss: StrictDecimal | None = None
    notional: StrictDecimal | None = None
    estimated_costs: StrictDecimal | None = None
    binding_constraint: str | None = None
    calculations: dict[str, Any] = Field(default_factory=dict)
    exposure_after: dict[str, Any] = Field(default_factory=dict)

    @property
    def reasons(self) -> list[Check]:
        decisive = {CheckOutcome.FAIL, CheckOutcome.UNKNOWN, CheckOutcome.HALT}
        return [c for c in self.checks if c.outcome in decisive]


__all__ = [
    "AssessmentInputs",
    "AssessmentOutcome",
    "Check",
    "Correlation",
    "HeldPosition",
    "InstrumentData",
    "MarketData",
    "PortfolioView",
    "Proposal",
    "ReasonCode",
    "ReservedExposure",
]

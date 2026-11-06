"""Risk configuration: hard limits and analytical preferences (spec 14.1, 14.4, 14.7).

A configuration version has two separate parts:

* :class:`HardLimits` bound what any proposal may do. They are the risk engine's authority:
  no strategy, model or LLM can change them; a change is a new immutable configuration
  version written by an authorized operator with a reason (``governance``).
* :class:`Preferences` are analytical estimates the engine uses to size conservatively
  (cost and slippage assumptions, how correlation is measured). They are versioned and
  operator-only too, but they never relax a hard limit: every hard limit is checked against
  the result whatever the preferences say.

The defaults are the Atlas starting values from the master specification (14.1) plus
conservative starting values for the limits spec 14.4 asks the framework to support. **They
are starting values, not optimal or recommended settings**, and nothing here is tuned.
Values are Decimals from strings; floats are refused. Fractions are fractions (``0.005`` is
0.5%); ``*_bps`` values are basis points.

No environment variable can change a limit: configuration lives only in the database
(``risk_configuration_versions``), so a deployment setting cannot loosen risk.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.accounting.money import MoneyError, to_decimal
from app.risk.enums import MissingCorrelationPolicy, MissingDataPolicy, UnclassifiedSectorPolicy

ASSET_CLASSES = ("EQUITY", "ETF", "CRYPTO", "FOREX")
DEFAULTS_NOTE = (
    "Initial Atlas starting values (spec 14.1 plus conservative framework defaults). They are "
    "not optimal or recommended settings and have not been tuned."
)


def _strict_decimal(value: object) -> Decimal:
    try:
        return to_decimal(value, "risk setting")
    except MoneyError as exc:
        raise ValueError(str(exc)) from None


StrictDecimal = Annotated[Decimal, BeforeValidator(_strict_decimal)]
Fraction = Annotated[StrictDecimal, Field(ge=0, le=1)]
PositiveFraction = Annotated[StrictDecimal, Field(gt=0, le=1)]
Bps = Annotated[StrictDecimal, Field(ge=0, le=10_000)]


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode("utf-8")).hexdigest()


class HardLimits(_Section):
    """Limits no proposal may exceed. Changed only by an authorized operator (D-139)."""

    # Spec 14.1 starting values.
    risk_per_trade_pct: Annotated[StrictDecimal, Field(gt=0, le=Decimal("0.05"))] = Decimal("0.005")
    max_open_positions: int = Field(default=8, ge=1, le=200)
    daily_loss_limit_pct: Annotated[StrictDecimal, Field(gt=0, le=Decimal("0.5"))] = Decimal("0.02")
    weekly_loss_limit_pct: Annotated[StrictDecimal, Field(gt=0, le=Decimal("0.5"))] = Decimal(
        "0.05"
    )
    max_drawdown_pct: Annotated[StrictDecimal, Field(gt=0, le=Decimal("0.9"))] = Decimal("0.10")
    # The ledger has no margin loan (D-127): leverage above 1x cannot be represented.
    max_leverage: Annotated[StrictDecimal, Field(gt=0, le=1)] = Decimal("1")
    max_gross_exposure_pct: PositiveFraction = Decimal("1.00")
    # Framework limits (spec 14.4) with conservative starting values.
    max_net_exposure_pct: PositiveFraction = Decimal("1.00")
    max_single_asset_pct: PositiveFraction = Decimal("0.20")
    # Gross exposure per asset class; an absent or zero class is not tradable.
    asset_class_limits_pct: dict[str, Fraction] = Field(
        default_factory=lambda: {
            "EQUITY": Decimal("1.00"),
            "ETF": Decimal("1.00"),
            "CRYPTO": Decimal("0"),
            "FOREX": Decimal("0"),
        }
    )
    max_sector_pct: PositiveFraction = Decimal("0.30")
    max_correlated_exposure_pct: PositiveFraction = Decimal("0.40")
    max_short_exposure_pct: Fraction = Decimal("0.30")
    # Short-sale collateral committed beyond the restricted proceeds, as a share of equity.
    max_margin_usage_pct: Fraction = Decimal("0.50")
    # Short selling (spec 14.6).
    shorts_enabled: bool = True
    # A short gets this fraction of the per-trade risk budget: a stop does not cap the loss
    # of a short that gaps through it, and the theoretical loss is unlimited.
    short_risk_budget_fraction: PositiveFraction = Decimal("0.5")
    # Minimum own-capital collateral of a short beyond its restricted proceeds (Reg T 150%
    # initial requirement = proceeds + 50%). A provider requirement above it is used instead.
    min_short_margin_rate: Annotated[StrictDecimal, Field(ge=0, le=5)] = Decimal("0.50")
    missing_margin_policy: MissingDataPolicy = MissingDataPolicy.REJECT
    # Stops.
    require_stop: bool = True
    min_stop_distance_bps: Bps = Decimal("10")
    # Entry must be within this distance of the current fresh price; a stale level is not a
    # trade at today's risk.
    max_entry_deviation_pct: PositiveFraction = Decimal("0.02")
    # Liquidity and trading cost limits.
    max_adv_participation_pct: PositiveFraction = Decimal("0.01")
    max_spread_bps: Bps = Decimal("50")
    # Correlation and classification when data is missing.
    missing_correlation_policy: MissingCorrelationPolicy = (
        MissingCorrelationPolicy.ASSUME_CORRELATED
    )
    unclassified_sector_policy: UnclassifiedSectorPolicy = UnclassifiedSectorPolicy.POOL
    # Accounting must have reconciled the current ledger (no entry posted since).
    require_current_reconciliation: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> HardLimits:
        if self.max_gross_exposure_pct > self.max_leverage:
            raise ValueError("max_gross_exposure_pct cannot exceed max_leverage")
        if self.max_net_exposure_pct > self.max_gross_exposure_pct:
            raise ValueError("max_net_exposure_pct cannot exceed max_gross_exposure_pct")
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError("weekly_loss_limit_pct cannot be below daily_loss_limit_pct")
        unknown = set(self.asset_class_limits_pct) - set(ASSET_CLASSES)
        if unknown:
            raise ValueError(f"unknown asset classes {sorted(unknown)}")
        return self

    def asset_class_limit(self, asset_class: str) -> Decimal:
        return self.asset_class_limits_pct.get(asset_class, Decimal(0))


class Preferences(_Section):
    """Analytical estimates used for conservative sizing. They never relax a hard limit.

    Every cost value is an ASSUMPTION recorded with each assessment, not a market quote.
    Amounts are in the instrument's quote currency.
    """

    commission_per_unit: Annotated[StrictDecimal, Field(ge=0, le=100)] = Decimal("0.005")
    min_commission: Annotated[StrictDecimal, Field(ge=0, le=1000)] = Decimal("1.00")
    fee_bps: Bps = Decimal("1")
    slippage_bps: Bps = Decimal("5")
    stop_slippage_bps: Bps = Decimal("10")
    # Used when no fresh quote is available to measure the spread.
    fallback_spread_bps: Bps = Decimal("25")
    quote_max_age_minutes: int = Field(default=30, ge=1, le=24 * 60)
    fx_conversion_cost_bps: Bps = Decimal("10")
    short_borrow_rate_annual: Annotated[StrictDecimal, Field(ge=0, le=5)] = Decimal("0.05")
    default_holding_days: int = Field(default=10, ge=1, le=365)
    # Used only when ``require_stop`` is off and a proposal has no stop.
    assumed_stop_distance_pct: PositiveFraction = Decimal("0.10")
    correlation_threshold: PositiveFraction = Decimal("0.70")
    correlation_max_age_days: int = Field(default=7, ge=1, le=90)
    liquidity_max_age_days: int = Field(default=7, ge=1, le=90)
    assessment_ttl_seconds: int = Field(default=300, ge=10, le=3600)


class RiskConfiguration(BaseModel):
    """One immutable configuration version as the engine uses it."""

    model_config = ConfigDict(frozen=True)

    version_id: str | None = None
    version: int = 0
    hard_limits: HardLimits = Field(default_factory=HardLimits)
    preferences: Preferences = Field(default_factory=Preferences)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def config_diff(
    previous: tuple[HardLimits, Preferences] | None, new: tuple[HardLimits, Preferences]
) -> list[dict[str, Any]]:
    """Field-by-field differences between two versions (all fields for the first one)."""
    changes: list[dict[str, Any]] = []
    for section, index in (("hard_limits", 0), ("preferences", 1)):
        after = new[index].canonical()
        before = previous[index].canonical() if previous is not None else {}
        for key in sorted(after):
            if previous is None or before.get(key) != after[key]:
                changes.append(
                    {
                        "section": section,
                        "field": key,
                        "previous": before.get(key),
                        "new": after[key],
                    }
                )
    return changes


__all__ = [
    "ASSET_CLASSES",
    "DEFAULTS_NOTE",
    "HardLimits",
    "Preferences",
    "RiskConfiguration",
    "canonical_json",
    "config_diff",
]

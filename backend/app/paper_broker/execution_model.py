"""Versioned execution-model assumptions of the simulated paper broker (Phase 7B).

**Simulated execution, not a replica of real markets.** Every number here is an assumption
an operator can change; it is not a measurement and not a claim about any venue. The
defaults are deliberately conservative (they make fills slower, smaller and more expensive
than they would often be), not unbiased: they were chosen so that a strategy does not look
better in paper trading than it plausibly could.

A model is content-addressed: :meth:`ExecutionModel.definition_sha256` hashes the full
definition, the row in ``execution_models`` is written once per hash, every paper broker
order records the version that works it, and every simulated fill the version that produced
it. Changing an assumption therefore creates a new version for new orders and never
rewrites how earlier fills were derived.

Assumption summary (per asset class, see :class:`AssetClassModel`):

* fills only during the venue's session (US equities/ETFs: the XNYS regular session);
* an execution delay: only data observed ``latency_seconds`` after the order reached the
  simulator can fill it (never the quote displayed when it arrived);
* marketable orders execute against the **touch** of the newest fresh quote (buy at the
  ask, sell at the bid), never the last trade, plus ``slippage_bps`` and a linear impact
  of ``impact_bps_at_full_participation`` x participation; each quote's displayed size can
  be used once per order, scaled by ``touch_participation``;
* resting limits fill only on trades printed **strictly through** the limit (or, without
  trades, completed bars that traded strictly through it), at the limit, for at most
  ``trade_participation`` / ``bar_participation`` of that volume: touching is not filling;
* with no fresh quote, ``allow_bar_fallback`` lets a market order use the newest completed
  bar's close widened by ``assumed_half_spread_bps`` (an assumed spread, labelled
  BAR_FALLBACK); otherwise, and whenever volume is unknown, execution is deferred;
* commissions per unit with an order minimum and a cap, or a rate of notional; sell-side
  regulatory fees; a short borrow rate. Rounded up to the currency's minor unit;
* version 2 (Phase 10B) adds the gap rule: a stop triggered by an observation that was
  already beyond it fills no better than that observation (adjusted by the slippage).

IEX limitation: Atlas's configured equity feed (IEX) carries only IEX's own quotes and
trades, a small share of consolidated volume. Displayed sizes and volumes are therefore
much smaller than the whole market's, and participation caps applied to them understate
the liquidity available: fills are slower and smaller than on a consolidated feed. IEX
quotes can also be absent for long stretches in less active symbols; the model then
defers or falls back to bars, as configured, rather than inventing a quote.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database.models.execution import ExecutionModelRecord

D = Decimal


class AssetClassModel(BaseModel):
    """Execution assumptions for one asset class (all amounts in the asset's currency)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    supported: bool = True
    # Session: True = fills only in the regular session of the venue's calendar (XNYS);
    # False = continuous (crypto).
    regular_session_only: bool = True
    # Data freshness: a quote older than this is not executable; a bar must have ended
    # within ``bar_max_age_seconds`` to be used for a fallback price.
    quote_max_age_seconds: int = Field(default=60, ge=1, le=3600)
    bar_max_age_seconds: int = Field(default=180, ge=60, le=86400)
    bar_timeframe: str = "1Min"
    # Execution delay: only observations at least this long after the order reached the
    # simulator (or its stop triggered) can execute it; never the quote displayed at arrival.
    latency_seconds: Decimal = Field(default=D("1"), ge=0, le=300)
    allow_bar_fallback: bool = True
    assumed_half_spread_bps: Decimal = Field(default=D("5"), ge=0, le=1000)
    # Price adjustments for marketable executions (adverse to the order).
    slippage_bps: Decimal = Field(default=D("2"), ge=0, le=1000)
    impact_bps_at_full_participation: Decimal = Field(default=D("10"), ge=0, le=10000)
    # Liquidity: shares of the measured liquidity an order may take per observation.
    touch_participation: Decimal = Field(default=D("0.5"), gt=0, le=1)
    trade_participation: Decimal = Field(default=D("0.1"), gt=0, le=1)
    bar_participation: Decimal = Field(default=D("0.05"), gt=0, le=1)
    # Costs.
    commission_per_unit: Decimal = Field(default=D("0.005"), ge=0)
    commission_rate: Decimal = Field(default=D("0"), ge=0, le=D("0.1"))
    commission_minimum: Decimal = Field(default=D("1.00"), ge=0)
    commission_maximum_rate: Decimal | None = Field(default=D("0.01"), ge=0, le=D("0.1"))
    sell_regulatory_fee_rate: Decimal = Field(default=D("0.0000278"), ge=0, le=D("0.01"))
    sell_per_unit_fee: Decimal = Field(default=D("0.000166"), ge=0)
    sell_per_unit_fee_maximum: Decimal | None = Field(default=D("8.30"), ge=0)
    exchange_fee_rate: Decimal = Field(default=D("0"), ge=0, le=D("0.01"))
    # Financing: annual borrow rate on short market value, accrued daily on this basis.
    borrow_rate_annual: Decimal = Field(default=D("0.03"), ge=0, le=D("2"))
    long_financing_rate_annual: Decimal = Field(default=D("0"), ge=0, le=D("2"))
    day_count: int = Field(default=360, ge=360, le=366)
    # Phase 10B gap rule (D-217): a stop whose trigger observation was already beyond the
    # stop (a gap) fills no better than that observation, adjusted adversely by the
    # slippage (for a bar: its open, within the bar's range), in the triggering evaluation
    # and every later one. False in version 1 (the cap is only the stop price). Omitted
    # from the definition when False, so the content hashes of earlier versions and of the
    # 8A backtest model do not change.
    gap_fill_at_observed: bool = False


GAP_FIELD = "gap_fill_at_observed"
EQUITY = AssetClassModel()
CRYPTO = AssetClassModel(
    regular_session_only=False,
    quote_max_age_seconds=30,
    assumed_half_spread_bps=D("10"),
    slippage_bps=D("5"),
    impact_bps_at_full_participation=D("20"),
    commission_per_unit=D("0"),
    commission_rate=D("0.0025"),
    commission_minimum=D("0"),
    commission_maximum_rate=None,
    sell_regulatory_fee_rate=D("0"),
    sell_per_unit_fee=D("0"),
    sell_per_unit_fee_maximum=None,
    borrow_rate_annual=D("0"),
    day_count=365,
)
UNSUPPORTED = AssetClassModel(supported=False)


class ExecutionModel(BaseModel):
    """A complete, versioned execution model. ``code`` + ``version`` name it for people;
    the content hash identifies it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(default="atlas-paper-conservative", pattern=r"^[a-z0-9-]{3,64}$")
    version: int = Field(default=1, ge=1)
    description: str = (
        "Conservative simulated execution: touch-of-quote fills with slippage and linear "
        "impact, strictly-through limit fills, participation caps, stop-first on ambiguous "
        "bars. Simulated; not a replica of any venue."
    )
    classes: dict[str, AssetClassModel] = Field(
        default_factory=lambda: {"EQUITY": EQUITY, "ETF": EQUITY, "CRYPTO": CRYPTO}
    )
    # Rules that are not numbers, recorded with the version for the audit trail.
    rules: tuple[str, ...] = (
        "fills use only observations available at or before the evaluation time",
        "trades and bars count only if they occurred after the order reached the simulator "
        "(or after its stop triggered)",
        "a buy never fills above its limit, a sell never below it",
        "touching a limit is not a fill: trades or bars must trade strictly through it",
        "stops trigger on the touch (bid for sells, ask for buys) or on trades and bar "
        "extremes through the stop; a triggered stop executes as a market order at the "
        "newest fresh quote and never better than its stop price (gaps fill worse)",
        "a triggered stop-limit may remain unfilled",
        "trailing stops ratchet only after the observations they are evaluated against "
        "(no intrabar ordering is assumed)",
        "an entry never fills beyond the reserved worst entry price",
        "an entry does not fill while a circuit breaker halts new entries",
        "each quote's displayed size is used at most once per order",
        "quantities are rounded down to the lot size; prices adversely to the tick",
        "commissions and fees are rounded up to the currency's minor unit",
    )

    def for_class(self, asset_class: str) -> AssetClassModel:
        return self.classes.get(asset_class, UNSUPPORTED)

    def definition(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.model_dump_json())
        for rules in out.get("classes", {}).values():
            if rules.get(GAP_FIELD) is False:
                rules.pop(GAP_FIELD)
        return out

    def definition_sha256(self) -> str:
        canonical = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def register(self, session: Session) -> int:
        """The id of this version in ``execution_models`` (written once per content hash)."""
        sha = self.definition_sha256()
        session.execute(
            pg_insert(ExecutionModelRecord)
            .values(
                code=self.code,
                version=self.version,
                definition=self.definition(),
                definition_sha256=sha,
                description=self.description,
            )
            .on_conflict_do_nothing()
        )
        found = session.scalar(
            select(ExecutionModelRecord.id).where(ExecutionModelRecord.definition_sha256 == sha)
        )
        if found is None:  # pragma: no cover  # (code, version) reused for other content
            raise ValueError(
                f"execution model {self.code} v{self.version} already exists with different "
                "assumptions: give the changed model a new version"
            )
        return int(found)


# Version 1 (Phase 7B): orders the simulator first evaluated before Phase 10B keep it.
MODEL_V1 = ExecutionModel()

GAP_RULE = (
    "gap rule: a stop triggered by an observation already beyond it (a trade, a fresh quote, "
    "or a bar that opened beyond it) fills no better than that observation adjusted "
    "adversely by the slippage (for a bar: its open, within the bar's range), now and in "
    "every later evaluation; fills at the stop price are never assumed"
)
# Version 2 (Phase 10B, D-217): version 1 plus the gap rule. New orders are worked under it.
DEFAULT_MODEL = ExecutionModel(
    version=2,
    description=(
        "Conservative simulated execution: touch-of-quote fills with slippage and linear "
        "impact, strictly-through limit fills, participation caps, stop-first on ambiguous "
        "bars, gap-through-stop fills no better than the first observed price beyond the "
        "stop. Simulated; not a replica of any venue."
    ),
    classes={
        name: rules.model_copy(update={GAP_FIELD: True}) for name, rules in MODEL_V1.classes.items()
    },
    rules=(*MODEL_V1.rules, GAP_RULE),
)

__all__ = [
    "CRYPTO",
    "DEFAULT_MODEL",
    "EQUITY",
    "GAP_FIELD",
    "GAP_RULE",
    "MODEL_V1",
    "AssetClassModel",
    "ExecutionModel",
]

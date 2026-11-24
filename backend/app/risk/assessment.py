"""The deterministic risk assessment (spec 14; decisions D-138 to D-144). A pure function.

``assess(inputs)`` decides from :class:`AssessmentInputs` alone: no database, no clock, no
randomness, no strategy score, model probability or LLM text. The same inputs always give
the same outcome, which is how stored assessments are replayed.

Order of evaluation (every check is recorded, passing or not):

1. circuit breakers: any active breaker -> RISK_HALTED (new entries blocked);
2. state: configuration present, portfolio marked to fresh prices, equity positive,
   reconciliation passed and current, loss state measurable, risk budget left;
3. the proposal: not expired, entry positive, stop present (when required), on the correct
   side and not too close;
4. the instrument: supported sizer, asset class enabled, tradable, currency supported,
   lot size known; for a short, shorts enabled, shortable, borrowable, margin known;
5. market data: fresh price and FX rate, entry near the market, stop not already breached,
   spread within limit, liquidity measured;
6. portfolio: no opposite position, open-position count;
7. sizing (only when nothing above failed): risk budget, then every capacity limit
   (capital, gross, leverage, net, single asset, asset class, sector, correlated cluster,
   short exposure, margin, liquidity), the smallest wins, floored to the lot and verified.

Result precedence: any HALT -> RISK_HALTED; else any FAIL -> REJECTED; else any UNKNOWN ->
INSUFFICIENT_DATA; else PASS_FOR_RESERVATION. A pass is **not** an execution approval: Phase
6C's atomic reservation must re-check under the account lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.accounting.money import ONE, ZERO, currency, div, money_context
from app.risk.config import HardLimits, Preferences
from app.risk.enums import (
    AssessmentResult,
    CheckOutcome,
    MarkStatus,
    MissingCorrelationPolicy,
    MissingDataPolicy,
    TradeDirection,
    UnclassifiedSectorPolicy,
)
from app.risk.models import (
    AssessmentInputs,
    AssessmentOutcome,
    Check,
    HeldPosition,
    ReasonCode,
)
from app.risk.sizing import (
    BPS,
    CostAssumptions,
    UnitEconomics,
    adverse_levels,
    capacity_quantity,
    ceil_to,
    floor_to,
    floor_to_increment,
    max_quantity_for_budget,
    sizer_for,
    to_functional_up,
    unit_economics,
    verified_quantity,
)

ENGINE_VERSION = "6B.1"
NOT_AN_APPROVAL = (
    "PASS_FOR_RESERVATION is not an execution approval: the atomic reservation and final "
    "order authorization belong to Phase 6C, which must re-check every limit under the "
    "account lock."
)
UNCLASSIFIED = "UNCLASSIFIED"


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole <= ZERO:
        return None
    return div(part, whole)


@dataclass
class _Capacity:
    code: str
    reason: ReasonCode
    quantity: Decimal  # unrounded units that fit
    measured: Decimal  # current usage (functional) or a measurement
    limit: Decimal  # the limit in the same unit
    unit: str
    details: dict[str, Any]


class _Assessment:
    def __init__(self, inputs: AssessmentInputs) -> None:
        self.i = inputs
        self.checks: list[Check] = []
        self.calc: dict[str, Any] = {"engine_version": ENGINE_VERSION}

    # ------------------------------------------------------------------ recording
    def add(
        self,
        code: str,
        outcome: CheckOutcome,
        message: str,
        reason: ReasonCode | None = None,
        *,
        measured: Decimal | None = None,
        limit: Decimal | None = None,
        unit: str | None = None,
        **details: Any,
    ) -> None:
        self.checks.append(
            Check(
                code=code,
                outcome=outcome,
                reason_code=reason,
                message=message,
                measured=measured,
                limit=limit,
                unit=unit,
                details={k: (str(v) if isinstance(v, Decimal) else v) for k, v in details.items()},
            )
        )

    def blocked(self) -> bool:
        return any(
            c.outcome in (CheckOutcome.HALT, CheckOutcome.FAIL, CheckOutcome.UNKNOWN)
            for c in self.checks
        )

    def outcome(self, **values: Any) -> AssessmentOutcome:
        outcomes = {c.outcome for c in self.checks}
        if CheckOutcome.HALT in outcomes:
            result = AssessmentResult.RISK_HALTED
        elif CheckOutcome.FAIL in outcomes:
            result = AssessmentResult.REJECTED
        elif CheckOutcome.UNKNOWN in outcomes:
            result = AssessmentResult.INSUFFICIENT_DATA
        else:
            result = AssessmentResult.PASS_FOR_RESERVATION
        if result is not AssessmentResult.PASS_FOR_RESERVATION:
            values["permitted_quantity"] = None
        self.calc["note"] = NOT_AN_APPROVAL
        return AssessmentOutcome(
            result=result, checks=self.checks, calculations=self.calc, **values
        )

    # ------------------------------------------------------------------ run
    def run(self) -> AssessmentOutcome:
        self._breakers()
        if self.i.config is None:
            self.add(
                "configuration",
                CheckOutcome.UNKNOWN,
                "no risk configuration version exists for this account: run "
                "`python -m app.risk.cli init`",
                ReasonCode.NO_CONFIGURATION,
            )
            return self.outcome()
        limits = self.i.config.hard_limits
        prefs = self.i.config.preferences
        self.calc["config_version"] = self.i.config.version
        self._state(limits)
        stop = self._proposal(limits, prefs)
        self._instrument(limits)
        margin_rate = self._short(limits) if self._is_short else ZERO
        spread = self._market(limits, prefs)
        self._portfolio(limits)
        if self.blocked() or stop is None or spread is None:
            return self.outcome()
        return self._size(limits, prefs, stop, spread, margin_rate)

    @property
    def _is_short(self) -> bool:
        return self.i.proposal.direction is TradeDirection.SHORT

    # ------------------------------------------------------------------ 1. breakers
    def _breakers(self) -> None:
        active = self.i.portfolio.active_breakers
        if not active:
            self.add("circuit_breakers", CheckOutcome.PASS, "no circuit breaker is active")
            return
        for breaker in active:
            self.add(
                "circuit_breakers",
                CheckOutcome.HALT,
                f"circuit breaker {breaker.value} is active: new entries are blocked until "
                "its recovery process completes",
                ReasonCode.BREAKER_ACTIVE,
                breaker=breaker.value,
            )

    # ------------------------------------------------------------------ 2. state
    def _state(self, limits: HardLimits) -> None:
        p = self.i.portfolio
        if p.mark_status is not MarkStatus.COMPLETE:
            self.add(
                "portfolio_marked",
                CheckOutcome.UNKNOWN,
                f"the portfolio could not be marked to fresh prices ({p.mark_status.value}): "
                "equity and exposure are not known at the assessment time",
                ReasonCode.PORTFOLIO_NOT_MARKED,
                issues=p.mark_issues,
            )
        else:
            self.add("portfolio_marked", CheckOutcome.PASS, "every position marked to fresh prices")
        if p.equity <= ZERO:
            self.add(
                "equity",
                CheckOutcome.FAIL,
                "equity is not positive",
                ReasonCode.EQUITY_NOT_POSITIVE,
                measured=p.equity,
                unit=p.functional_currency,
            )
        if p.reconciliation_status is None:
            self.add(
                "reconciliation",
                CheckOutcome.UNKNOWN,
                "no accounting reconciliation has run for this account",
                ReasonCode.RECONCILIATION_MISSING,
            )
        elif limits.require_current_reconciliation and not p.reconciliation_current:
            self.add(
                "reconciliation",
                CheckOutcome.UNKNOWN,
                "entries were posted since the last reconciliation: reconcile before assessing",
                ReasonCode.RECONCILIATION_NOT_CURRENT,
                status=p.reconciliation_status,
            )
        else:
            self.add(
                "reconciliation",
                CheckOutcome.PASS,
                f"latest reconciliation {p.reconciliation_status}"
                + (" and current" if p.reconciliation_current else ""),
            )
        if p.daily_loss is None or p.weekly_loss is None or p.drawdown is None:
            self.add(
                "loss_state",
                CheckOutcome.UNKNOWN,
                "daily loss, weekly loss or drawdown could not be measured",
                ReasonCode.LOSS_STATE_UNKNOWN,
            )
        if p.available_risk_budget is not None and p.available_risk_budget <= ZERO:
            self.add(
                "available_risk_budget",
                CheckOutcome.FAIL,
                "no loss allowance is left before a loss limit",
                ReasonCode.RISK_BUDGET_EXHAUSTED,
                measured=p.available_risk_budget,
                unit=p.functional_currency,
            )

    # ------------------------------------------------------------------ 3. proposal
    def _proposal(self, limits: HardLimits, prefs: Preferences) -> Decimal | None:
        pr = self.i.proposal
        if pr.expires_at is not None and pr.expires_at <= self.i.assessed_at:
            self.add(
                "proposal_expiry",
                CheckOutcome.FAIL,
                f"the proposal expired at {pr.expires_at.isoformat()}",
                ReasonCode.PROPOSAL_EXPIRED,
            )
        if pr.entry <= ZERO:
            self.add("entry", CheckOutcome.FAIL, "entry must be positive", ReasonCode.INVALID_ENTRY)
            return None
        if pr.requested_quantity is not None and pr.requested_quantity <= ZERO:
            self.add(
                "requested_quantity",
                CheckOutcome.FAIL,
                "a requested quantity must be positive",
                ReasonCode.INVALID_REQUESTED_QUANTITY,
            )
        stop = pr.stop
        if stop is None:
            if limits.require_stop:
                self.add(
                    "stop",
                    CheckOutcome.FAIL,
                    "a stop level is required by policy and the proposal has none",
                    ReasonCode.MISSING_STOP,
                )
                return None
            with money_context():
                sign = -ONE if pr.direction is TradeDirection.LONG else ONE
                stop = pr.entry * (ONE + sign * prefs.assumed_stop_distance_pct)
            self.add(
                "stop",
                CheckOutcome.INFO,
                f"no stop given; ASSUMED stop {stop} at {prefs.assumed_stop_distance_pct} of "
                "entry (require_stop is off)",
                ReasonCode.STOP_ASSUMED,
            )
        if stop <= ZERO:
            self.add("stop", CheckOutcome.FAIL, "stop must be positive", ReasonCode.STOP_WRONG_SIDE)
            return None
        if pr.direction is TradeDirection.LONG and stop >= pr.entry:
            self.add(
                "stop",
                CheckOutcome.FAIL,
                f"a long stop must be below the entry (entry {pr.entry}, stop {stop})",
                ReasonCode.STOP_WRONG_SIDE,
            )
            return None
        if pr.direction is TradeDirection.SHORT and stop <= pr.entry:
            self.add(
                "stop",
                CheckOutcome.FAIL,
                f"a short stop must be above the entry (entry {pr.entry}, stop {stop})",
                ReasonCode.STOP_WRONG_SIDE,
            )
            return None
        distance_bps = div(abs(pr.entry - stop), pr.entry) / BPS
        if distance_bps < limits.min_stop_distance_bps:
            self.add(
                "stop_distance",
                CheckOutcome.FAIL,
                "the stop is closer to the entry than the minimum distance",
                ReasonCode.STOP_TOO_CLOSE,
                measured=distance_bps,
                limit=limits.min_stop_distance_bps,
                unit="bps",
            )
            return None
        self.add(
            "stop",
            CheckOutcome.PASS,
            "stop present, on the correct side and far enough",
            measured=distance_bps,
            limit=limits.min_stop_distance_bps,
            unit="bps",
        )
        return stop

    # ------------------------------------------------------------------ 4. instrument
    def _instrument(self, limits: HardLimits) -> None:
        ins = self.i.instrument
        if ins is None:
            self.add(
                "instrument",
                CheckOutcome.FAIL,
                "the asset is unknown",
                ReasonCode.INSTRUMENT_NOT_SUPPORTED,
            )
            return
        sizer = sizer_for(ins.asset_class)
        if sizer is None or not sizer.supported:
            self.add(
                "instrument",
                CheckOutcome.FAIL,
                f"{ins.asset_class} cannot be sized: "
                + (sizer.unsupported_reason if sizer else "no sizer exists"),
                ReasonCode.INSTRUMENT_NOT_SUPPORTED,
            )
            return
        class_limit = limits.asset_class_limit(ins.asset_class)
        if class_limit <= ZERO:
            self.add(
                "asset_class_enabled",
                CheckOutcome.FAIL,
                f"asset class {ins.asset_class} has a zero exposure limit (not enabled)",
                ReasonCode.ASSET_CLASS_NOT_ENABLED,
                measured=ZERO,
                limit=class_limit,
            )
        if ins.asset_status != "ACTIVE":
            self.add(
                "tradable",
                CheckOutcome.FAIL,
                f"the asset is {ins.asset_status}",
                ReasonCode.INSTRUMENT_NOT_TRADABLE,
            )
        elif not ins.reference_known or ins.tradable is None:
            self.add(
                "tradable",
                CheckOutcome.UNKNOWN,
                "no provider reference says the instrument is tradable",
                ReasonCode.TRADABILITY_UNKNOWN,
            )
        elif not ins.tradable:
            self.add(
                "tradable",
                CheckOutcome.FAIL,
                "the provider lists the instrument as not tradable",
                ReasonCode.INSTRUMENT_NOT_TRADABLE,
            )
        else:
            self.add(
                "tradable",
                CheckOutcome.PASS,
                f"tradable per provider reference version {ins.reference_version}",
            )
        if not ins.currency_supported:
            self.add(
                "currency",
                CheckOutcome.FAIL,
                f"quote currency {ins.quote_currency} is not a ledger currency",
                ReasonCode.UNSUPPORTED_CURRENCY,
            )
        if ins.quantity_increment is None or ins.min_quantity is None:
            self.add(
                "lot_size",
                CheckOutcome.UNKNOWN,
                "no instrument specification (lot size) is registered: lot sizes are never guessed",
                ReasonCode.LOT_SIZE_MISSING,
            )
        else:
            self.add(
                "lot_size",
                CheckOutcome.PASS,
                f"lot increment {ins.quantity_increment}, minimum {ins.min_quantity}",
                spec_id=ins.spec_id,
            )

    # ------------------------------------------------------------------ short selling
    def _short(self, limits: HardLimits) -> Decimal:
        ins = self.i.instrument
        if not limits.shorts_enabled:
            self.add(
                "short_enabled",
                CheckOutcome.FAIL,
                "short selling is disabled by the hard limits",
                ReasonCode.SHORTS_DISABLED,
            )
            return ZERO
        if ins is None:
            return ZERO
        sizer = sizer_for(ins.asset_class)
        if sizer is not None and not sizer.supports_short:
            self.add(
                "short_supported",
                CheckOutcome.FAIL,
                f"{ins.asset_class} cannot be shorted by Atlas",
                ReasonCode.SHORT_NOT_SUPPORTED,
            )
            return ZERO
        if not ins.reference_known or ins.shortable is None:
            self.add(
                "shortable",
                CheckOutcome.FAIL,
                "shortability is not reported: an asset is never assumed shortable",
                ReasonCode.SHORTABILITY_UNKNOWN,
            )
        elif not ins.shortable:
            self.add(
                "shortable",
                CheckOutcome.FAIL,
                "the provider lists the asset as not shortable",
                ReasonCode.NOT_SHORTABLE,
            )
        else:
            self.add("shortable", CheckOutcome.PASS, "shortable per provider reference")
        if ins.easy_to_borrow is not True:
            self.add(
                "borrow",
                CheckOutcome.FAIL,
                "borrow availability is "
                + ("not reported" if ins.easy_to_borrow is None else "hard to borrow")
                + ": shorts need an easy-to-borrow asset",
                ReasonCode.BORROW_UNAVAILABLE,
            )
        else:
            self.add("borrow", CheckOutcome.PASS, "easy to borrow per provider reference")
        floor_rate = limits.min_short_margin_rate
        if ins.margin_requirement_short is None:
            if limits.missing_margin_policy is MissingDataPolicy.REJECT:
                self.add(
                    "short_margin",
                    CheckOutcome.FAIL,
                    "the provider reports no short margin requirement and policy rejects "
                    "missing margin data",
                    ReasonCode.MARGIN_DATA_MISSING,
                )
                return floor_rate
            self.add(
                "short_margin",
                CheckOutcome.INFO,
                f"no provider short margin requirement: the configured minimum {floor_rate} "
                "is used (ASSUMPTION per policy)",
                ReasonCode.MARGIN_RATE_ASSUMED,
            )
            return floor_rate
        rate = max(floor_rate, ins.margin_requirement_short)
        self.add(
            "short_margin",
            CheckOutcome.PASS,
            f"short collateral beyond proceeds: {rate} of notional (provider "
            f"{ins.margin_requirement_short}, minimum {floor_rate})",
            measured=rate,
        )
        return rate

    # ------------------------------------------------------------------ 5. market data
    def _market(self, limits: HardLimits, prefs: Preferences) -> Decimal | None:
        m = self.i.market
        pr = self.i.proposal
        ins = self.i.instrument
        functional = self.i.portfolio.functional_currency
        if m.price_status != "FRESH" or m.price is None:
            reason = ReasonCode.PRICE_MISSING if m.price is None else ReasonCode.PRICE_STALE
            self.add(
                "price_freshness",
                CheckOutcome.UNKNOWN,
                f"no fresh price for the asset ({m.price_status}"
                + (f": {m.price_reason}" if m.price_reason else "")
                + ")",
                reason,
                observed_at=m.price_observed_at.isoformat() if m.price_observed_at else None,
            )
        else:
            self.add(
                "price_freshness",
                CheckOutcome.PASS,
                f"fresh {m.price_source} price {m.price}",
                observed_at=m.price_observed_at.isoformat() if m.price_observed_at else None,
            )
            deviation = div(abs(pr.entry - m.price), m.price)
            if deviation > limits.max_entry_deviation_pct:
                self.add(
                    "entry_vs_market",
                    CheckOutcome.FAIL,
                    f"entry {pr.entry} is too far from the current price {m.price}",
                    ReasonCode.ENTRY_DEVIATES_FROM_MARKET,
                    measured=deviation,
                    limit=limits.max_entry_deviation_pct,
                    unit="fraction",
                )
            else:
                self.add(
                    "entry_vs_market",
                    CheckOutcome.PASS,
                    "entry is near the current price",
                    measured=deviation,
                    limit=limits.max_entry_deviation_pct,
                    unit="fraction",
                )
            stop = pr.stop
            if stop is not None:
                breached = (pr.direction is TradeDirection.LONG and m.price <= stop) or (
                    pr.direction is TradeDirection.SHORT and m.price >= stop
                )
                if breached:
                    self.add(
                        "stop_vs_market",
                        CheckOutcome.FAIL,
                        f"the current price {m.price} is already beyond the stop {stop}",
                        ReasonCode.STOP_ALREADY_BREACHED,
                    )
        if ins is not None and ins.quote_currency != functional and ins.currency_supported:
            if m.fx_status != "FRESH" or m.fx_rate is None:
                self.add(
                    "fx_rate",
                    CheckOutcome.UNKNOWN,
                    f"no fresh {ins.quote_currency}/{functional} rate ({m.fx_status}"
                    + (f": {m.fx_reason}" if m.fx_reason else "")
                    + ")",
                    ReasonCode.FX_RATE_MISSING if m.fx_rate is None else ReasonCode.FX_RATE_STALE,
                )
            else:
                self.add(
                    "fx_rate",
                    CheckOutcome.PASS,
                    f"fresh rate {m.fx_rate} {functional} per {ins.quote_currency}",
                    fx_rate_id=m.fx_rate_id,
                )
        spread: Decimal | None
        if m.quote_fresh and m.bid is not None and m.ask is not None and m.bid > ZERO:
            mid = (m.bid + m.ask) / 2
            spread = div(m.ask - m.bid, mid)
            source = "measured from a fresh quote"
        else:
            spread = prefs.fallback_spread_bps * BPS
            source = "ASSUMED: no fresh quote"
            self.add(
                "spread_source",
                CheckOutcome.INFO,
                f"no fresh quote: spread ASSUMED at {prefs.fallback_spread_bps} bps",
                ReasonCode.SPREAD_ASSUMED,
            )
        spread_bps = spread / BPS
        if spread_bps > limits.max_spread_bps:
            self.add(
                "spread",
                CheckOutcome.FAIL,
                f"spread {source} is wider than the limit",
                ReasonCode.SPREAD_TOO_WIDE,
                measured=spread_bps,
                limit=limits.max_spread_bps,
                unit="bps",
            )
        else:
            self.add(
                "spread",
                CheckOutcome.PASS,
                f"spread {source} is within the limit",
                measured=spread_bps,
                limit=limits.max_spread_bps,
                unit="bps",
            )
        if m.adv_quote is None or m.adv_quote <= ZERO:
            self.add(
                "liquidity_data",
                CheckOutcome.UNKNOWN,
                "no current daily dollar-volume measurement for the asset",
                ReasonCode.LIQUIDITY_DATA_MISSING,
            )
        return spread

    # ------------------------------------------------------------------ 6. portfolio
    def _held(self) -> HeldPosition | None:
        for pos in self.i.portfolio.positions:
            if pos.asset_id == self.i.proposal.asset_id:
                return pos
        return None

    def _portfolio(self, limits: HardLimits) -> None:
        held = self._held()
        if held is not None and held.direction is not self.i.proposal.direction:
            self.add(
                "opposite_position",
                CheckOutcome.FAIL,
                f"a {held.direction.value} position in the asset is held: reduce or close it "
                "first (risk-reducing operations belong to the execution phases)",
                ReasonCode.OPPOSITE_POSITION_HELD,
            )
        count = len(self.i.portfolio.positions)
        after = count if held is not None else count + 1
        if after > limits.max_open_positions:
            self.add(
                "open_positions",
                CheckOutcome.FAIL,
                f"{after} open positions would exceed the maximum",
                ReasonCode.MAX_OPEN_POSITIONS,
                measured=Decimal(after),
                limit=Decimal(limits.max_open_positions),
                unit="positions",
            )
        else:
            self.add(
                "open_positions",
                CheckOutcome.PASS,
                f"{after} open position(s) after the trade",
                measured=Decimal(after),
                limit=Decimal(limits.max_open_positions),
                unit="positions",
            )
        ins = self.i.instrument
        if (
            ins is not None
            and ins.sector is None
            and limits.unclassified_sector_policy is UnclassifiedSectorPolicy.REJECT
        ):
            self.add(
                "sector_classification",
                CheckOutcome.FAIL,
                "the asset has no sector classification and policy rejects unclassified assets",
                ReasonCode.SECTOR_UNCLASSIFIED,
            )

    # ------------------------------------------------------------------ 7. sizing
    def _costs(self, prefs: Preferences, spread: Decimal) -> CostAssumptions:
        ins = self.i.instrument
        assert ins is not None  # noqa: S101  # checked before sizing
        foreign = ins.quote_currency != self.i.portfolio.functional_currency
        holding = self.i.proposal.expected_holding_days or prefs.default_holding_days
        return CostAssumptions(
            half_spread=spread / 2,
            entry_slippage=prefs.slippage_bps * BPS,
            exit_slippage=prefs.stop_slippage_bps * BPS,
            fee_rate=prefs.fee_bps * BPS,
            fx_cost_rate=prefs.fx_conversion_cost_bps * BPS if foreign else ZERO,
            commission_per_unit=prefs.commission_per_unit,
            min_commission=prefs.min_commission,
            borrow_rate_annual=prefs.short_borrow_rate_annual if self._is_short else ZERO,
            holding_days=holding,
        )

    def _size(
        self,
        limits: HardLimits,
        prefs: Preferences,
        stop: Decimal,
        spread: Decimal,
        margin_rate: Decimal,
    ) -> AssessmentOutcome:
        p = self.i.portfolio
        pr = self.i.proposal
        ins = self.i.instrument
        m = self.i.market
        assert ins is not None  # noqa: S101  # checked before sizing
        assert m.price is not None  # noqa: S101  # checked before sizing
        assert ins.quantity_increment is not None  # noqa: S101  # checked before sizing
        assert ins.min_quantity is not None  # noqa: S101  # checked before sizing
        functional = p.functional_currency
        f_places = currency(functional).minor_units
        rate = ONE if ins.quote_currency == functional else m.fx_rate
        assert rate is not None  # noqa: S101  # checked before sizing
        sizer = sizer_for(ins.asset_class)
        assert sizer is not None  # noqa: S101  # checked before sizing
        tick = sizer.tick_size(ins.provider_tick_size, pr.entry)
        if tick is None:
            self.add(
                "tick_size",
                CheckOutcome.UNKNOWN,
                f"no price increment is known for this {ins.asset_class} instrument",
                ReasonCode.TICK_SIZE_MISSING,
            )
            return self.outcome()
        entry_adj, stop_adj = adverse_levels(pr.direction, pr.entry, stop, tick)
        if entry_adj <= ZERO or stop_adj <= ZERO or entry_adj == stop_adj:
            self.add(
                "stop",
                CheckOutcome.FAIL,
                "after rounding to the tick the stop and entry coincide or are not positive",
                ReasonCode.STOP_WRONG_SIDE,
            )
            return self.outcome()
        costs = self._costs(prefs, spread)
        unit = unit_economics(pr.direction, entry_adj, stop_adj, costs, ins.multiplier)
        self.add(
            "cost_assumptions",
            CheckOutcome.INFO,
            "costs are ASSUMPTIONS from the configured preferences, not quotes",
            ReasonCode.COST_ASSUMPTIONS,
            half_spread=costs.half_spread,
            entry_slippage=costs.entry_slippage,
            exit_slippage=costs.exit_slippage,
            fee_rate=costs.fee_rate,
            fx_cost_rate=costs.fx_cost_rate,
            commission_per_unit=costs.commission_per_unit,
            min_commission=costs.min_commission,
            borrow_rate_annual=costs.borrow_rate_annual,
            holding_days=costs.holding_days,
        )
        if self._is_short:
            self.add(
                "borrow_cost",
                CheckOutcome.INFO,
                f"borrow cost ASSUMED at {costs.borrow_rate_annual} a year for "
                f"{costs.holding_days} days (no borrow-rate data source exists)",
                ReasonCode.BORROW_COST_ASSUMED,
            )
        if p.pending_exposure == ZERO:
            self.add(
                "pending_exposure",
                CheckOutcome.INFO,
                f"pending exposure 0 ({p.pending_source})",
                ReasonCode.PENDING_EXPOSURE,
            )

        # ---- risk budget
        base_pct = limits.risk_per_trade_pct
        if self._is_short:
            base_pct = base_pct * limits.short_risk_budget_fraction
        with money_context():
            base_budget = floor_to(p.equity * base_pct, f_places)
        budget = base_budget
        if p.available_risk_budget is not None:
            budget = min(budget, floor_to(p.available_risk_budget, f_places))
        self.add(
            "risk_budget",
            CheckOutcome.RESTRICT if budget < base_budget else CheckOutcome.PASS,
            (
                f"risk budget {budget} {functional} = equity {p.equity} x {base_pct}"
                + (
                    f", capped by the loss allowance left ({p.available_risk_budget})"
                    if budget < base_budget
                    else ""
                )
            ),
            ReasonCode.RISK_BUDGET,
            measured=budget,
            limit=base_budget,
            unit=functional,
        )
        q_risk = max_quantity_for_budget(budget, unit, costs, rate)
        self.calc.update(
            {
                "tick_size": str(tick),
                "entry": str(pr.entry),
                "stop": str(stop),
                "entry_adjusted": str(entry_adj),
                "stop_adjusted": str(stop_adj),
                "price_distance": str(abs(entry_adj - stop_adj)),
                "fx_rate": str(rate),
                "spread": str(spread),
                "unit": {
                    "entry_fill": str(unit.entry_fill),
                    "exit_fill": str(unit.exit_fill),
                    "price_loss": str(unit.price_loss),
                    "fees": str(unit.fees),
                    "fx_cost": str(unit.fx_cost),
                    "borrow_cost": str(unit.borrow_cost),
                    "variable_loss": str(unit.variable_loss),
                    "exposure_price": str(unit.exposure_price),
                },
                "risk_budget": str(budget),
                "risk_budget_base": str(base_budget),
                "raw_quantity_initial": str(div(budget, abs(entry_adj - stop_adj) * rate)),
                "raw_quantity_with_costs": str(q_risk),
            }
        )
        if q_risk <= ZERO:
            self.add(
                "risk_budget_costs",
                CheckOutcome.FAIL,
                "the risk budget does not even cover the minimum commissions",
                ReasonCode.COSTS_EXCEED_BUDGET,
                measured=budget,
                unit=functional,
            )
            return self.outcome(risk_budget=budget)

        # ---- capacity limits
        capacities = self._capacities(limits, unit, costs, rate, margin_rate)
        candidates: list[tuple[Decimal, str, ReasonCode | None]] = [
            (q_risk, "risk_budget", ReasonCode.RISK_BUDGET)
        ]
        candidates += [(c.quantity, c.code, c.reason) for c in capacities]
        if pr.requested_quantity is not None:
            candidates.append(
                (pr.requested_quantity, "requested_quantity", ReasonCode.REQUESTED_QUANTITY_REDUCED)
            )
        smallest, binding, binding_reason = min(candidates, key=lambda c: (c[0], c[1]))
        quantity, loss = verified_quantity(
            smallest,
            ins.quantity_increment,
            budget,
            unit,
            costs,
            rate,
            ins.quote_currency,
            functional,
        )
        self.calc.update(
            {
                "capacity_quantities": {c.code: str(c.quantity) for c in capacities},
                "binding_constraint": binding,
                "quantity_before_lot_rounding": str(smallest),
                "lot_increment": str(ins.quantity_increment),
            }
        )
        too_small = quantity < ins.min_quantity or quantity <= ZERO
        for c in capacities:
            is_binding = c.code == binding
            if is_binding and too_small:
                outcome = CheckOutcome.FAIL
            elif is_binding:
                outcome = CheckOutcome.RESTRICT
            else:
                outcome = CheckOutcome.PASS
            self.add(
                c.code,
                outcome,
                (
                    f"{c.code} permits {floor_to_increment(c.quantity, ins.quantity_increment)}"
                    f" units" + (" (binding)" if is_binding else "")
                ),
                c.reason if outcome is not CheckOutcome.PASS else None,
                measured=c.measured,
                limit=c.limit,
                unit=c.unit,
                **c.details,
            )
        if too_small:
            if binding in ("risk_budget", "requested_quantity"):
                self.add(
                    binding,
                    CheckOutcome.FAIL,
                    f"{binding} permits less than the minimum quantity {ins.min_quantity}",
                    ReasonCode.INVALID_REQUESTED_QUANTITY
                    if binding == "requested_quantity"
                    else binding_reason,
                )
            self.add(
                "minimum_quantity",
                CheckOutcome.FAIL,
                f"the permissible quantity {quantity} is below the instrument minimum "
                f"{ins.min_quantity} (binding: {binding})",
                ReasonCode.BELOW_MINIMUM_QUANTITY,
                measured=quantity,
                limit=ins.min_quantity,
                unit="units",
            )
            return self.outcome(risk_budget=budget, binding_constraint=binding)
        if pr.requested_quantity is not None and quantity < pr.requested_quantity:
            self.add(
                "requested_quantity",
                CheckOutcome.RESTRICT,
                f"requested {pr.requested_quantity}, permitted {quantity}",
                ReasonCode.REQUESTED_QUANTITY_REDUCED,
                measured=pr.requested_quantity,
                limit=quantity,
                unit="units",
            )

        # ---- results (rounded against Atlas)
        with money_context():
            notional_quote = quantity * unit.exposure_price * ins.multiplier
            distance_loss_quote = quantity * abs(entry_adj - stop_adj) * ins.multiplier
        notional = to_functional_up(notional_quote, rate, ins.quote_currency, functional)
        distance_loss = floor_to(
            ceil_to(distance_loss_quote, currency(ins.quote_currency).minor_units) * rate,
            f_places,
        )
        estimated_costs = max(loss - distance_loss, ZERO)
        self.add(
            "potential_loss",
            CheckOutcome.PASS,
            f"potential loss at the stop {loss} {functional} <= budget {budget}",
            measured=loss,
            limit=budget,
            unit=functional,
        )
        self.calc.update(
            {
                "permitted_quantity": str(quantity),
                "potential_loss": str(loss),
                "notional": str(notional),
                "estimated_costs": str(estimated_costs),
            }
        )
        return self.outcome(
            permitted_quantity=quantity,
            risk_budget=budget,
            potential_loss=loss,
            notional=notional,
            estimated_costs=estimated_costs,
            binding_constraint=binding,
            exposure_after=self._exposure_after(notional, margin_rate),
        )

    # ------------------------------------------------------------------ capacities
    def _capacities(
        self,
        limits: HardLimits,
        unit: UnitEconomics,
        costs: CostAssumptions,
        rate: Decimal,
        margin_rate: Decimal,
    ) -> list[_Capacity]:
        p = self.i.portfolio
        ins = self.i.instrument
        m = self.i.market
        assert ins is not None  # noqa: S101  # checked before sizing
        e = p.equity
        functional = p.functional_currency
        mult = ins.multiplier
        pending = p.pending_exposure
        with money_context():
            exposure_unit = unit.exposure_price * mult * rate
            fixed = costs.min_commission * rate
        caps: list[_Capacity] = []

        def cap(
            code: str,
            reason: ReasonCode,
            capacity: Decimal,
            per_unit: Decimal,
            used: Decimal,
            limit: Decimal,
            unit_label: str = functional,
            fixed_cost: Decimal = ZERO,
            **details: Any,
        ) -> None:
            caps.append(
                _Capacity(
                    code,
                    reason,
                    capacity_quantity(capacity, per_unit, fixed_cost),
                    used,
                    limit,
                    unit_label,
                    details,
                )
            )

        # Available capital: a long pays notional and entry costs; a short commits collateral
        # beyond its restricted proceeds plus entry costs. Neither may use more than is free.
        with money_context():
            if self._is_short:
                capital_unit = (
                    margin_rate * unit.exposure_price * mult
                    + unit.entry_costs
                    + costs.commission_per_unit
                ) * rate
            else:
                capital_unit = (
                    unit.entry_fill * mult + unit.entry_costs + costs.commission_per_unit
                ) * rate
        free = p.available_capital - pending
        cap(
            "available_capital",
            ReasonCode.AVAILABLE_CAPITAL,
            free,
            capital_unit,
            free,
            free,
            fixed_cost=fixed,
        )
        with money_context():
            gross_limit = limits.max_gross_exposure_pct * e
            leverage_limit = limits.max_leverage * e
            net_limit = limits.max_net_exposure_pct * e
        cap(
            "gross_exposure",
            ReasonCode.GROSS_EXPOSURE,
            gross_limit - p.gross_exposure - pending,
            exposure_unit,
            p.gross_exposure + pending,
            gross_limit,
        )
        cap(
            "leverage",
            ReasonCode.LEVERAGE,
            leverage_limit - p.gross_exposure - pending,
            exposure_unit,
            p.gross_exposure + pending,
            leverage_limit,
        )
        net_room = net_limit + p.net_exposure if self._is_short else net_limit - p.net_exposure
        cap(
            "net_exposure",
            ReasonCode.NET_EXPOSURE,
            net_room,
            exposure_unit,
            p.net_exposure,
            net_limit,
        )
        held = self._held()
        held_exposure = held.exposure if held is not None else ZERO
        with money_context():
            single_limit = limits.max_single_asset_pct * e
            class_limit = limits.asset_class_limit(ins.asset_class) * e
            sector_limit = limits.max_sector_pct * e
            corr_limit = limits.max_correlated_exposure_pct * e
        cap(
            "single_asset_exposure",
            ReasonCode.SINGLE_ASSET_EXPOSURE,
            single_limit - held_exposure,
            exposure_unit,
            held_exposure,
            single_limit,
        )
        class_used = sum(
            (q.exposure for q in p.positions if q.asset_class == ins.asset_class), ZERO
        )
        cap(
            "asset_class_exposure",
            ReasonCode.ASSET_CLASS_EXPOSURE,
            class_limit - class_used,
            exposure_unit,
            class_used,
            class_limit,
            asset_class=ins.asset_class,
        )
        sector = ins.sector or UNCLASSIFIED
        sector_used = sum(
            (q.exposure for q in p.positions if (q.sector or UNCLASSIFIED) == sector), ZERO
        )
        cap(
            "sector_exposure",
            ReasonCode.SECTOR_EXPOSURE,
            sector_limit - sector_used,
            exposure_unit,
            sector_used,
            sector_limit,
            sector=sector,
            classified=ins.sector is not None,
        )
        correlated, members = self._correlated()
        cap(
            "correlated_exposure",
            ReasonCode.CORRELATED_EXPOSURE,
            corr_limit - correlated,
            exposure_unit,
            correlated,
            corr_limit,
            members=members,
        )
        if self._is_short:
            with money_context():
                short_limit = limits.max_short_exposure_pct * e
                margin_limit = limits.max_margin_usage_pct * e
                margin_unit = margin_rate * exposure_unit
            cap(
                "short_exposure",
                ReasonCode.SHORT_EXPOSURE,
                short_limit - p.short_exposure,
                exposure_unit,
                p.short_exposure,
                short_limit,
            )
            if margin_unit > ZERO:
                cap(
                    "margin_usage",
                    ReasonCode.MARGIN_USAGE,
                    margin_limit - p.margin_used,
                    margin_unit,
                    p.margin_used,
                    margin_limit,
                    margin_rate=margin_rate,
                )
        assert m.adv_quote is not None  # noqa: S101  # checked before sizing
        with money_context():
            adv_functional = m.adv_quote * rate
            liquidity_limit = limits.max_adv_participation_pct * adv_functional
        cap(
            "liquidity",
            ReasonCode.LIQUIDITY,
            liquidity_limit,
            exposure_unit,
            adv_functional,
            liquidity_limit,
            adv_metric=m.adv_metric,
            adv_as_of=m.adv_as_of.isoformat() if m.adv_as_of else None,
        )
        return caps

    def _correlated(self) -> tuple[Decimal, list[dict[str, Any]]]:
        """Exposure of held positions economically linked to the candidate (not netted).

        A held position counts when it is the same asset, shares the operator's correlation
        cluster, or its measured |correlation| reaches the threshold; with no usable
        measurement it counts under ASSUME_CORRELATED. Long and short exposures add up:
        offsetting positions are never assumed to cancel.
        """
        cfg = self.i.config
        assert cfg is not None  # noqa: S101  # checked before sizing
        ins = self.i.instrument
        assert ins is not None  # noqa: S101  # checked before sizing
        threshold = cfg.preferences.correlation_threshold
        policy = cfg.hard_limits.missing_correlation_policy
        measured = {c.asset_id: c for c in self.i.correlations}
        total = ZERO
        members: list[dict[str, Any]] = []
        for pos in self.i.portfolio.positions:
            why: str | None = None
            if pos.asset_id == ins.asset_id:
                why = "same asset"
            elif ins.cluster is not None and pos.cluster == ins.cluster:
                why = f"cluster {ins.cluster}"
            else:
                corr = measured.get(pos.asset_id)
                if corr is not None and corr.value is not None:
                    if abs(corr.value) >= threshold:
                        why = f"|correlation| {abs(corr.value)} >= {threshold}"
                elif policy is MissingCorrelationPolicy.ASSUME_CORRELATED:
                    why = "no correlation measurement (ASSUMED correlated)"
            if why is not None:
                total += pos.exposure
                members.append({"asset_id": str(pos.asset_id), "symbol": pos.symbol, "why": why})
        return total, members

    def _exposure_after(self, notional: Decimal, margin_rate: Decimal) -> dict[str, Any]:
        p = self.i.portfolio
        long_after = p.long_exposure + (ZERO if self._is_short else notional)
        short_after = p.short_exposure + (notional if self._is_short else ZERO)
        with money_context():
            margin_after = p.margin_used + (margin_rate * notional if self._is_short else ZERO)
        gross_after = long_after + short_after
        net_after = long_after - short_after
        values = {
            "long_exposure": long_after,
            "short_exposure": short_after,
            "gross_exposure": gross_after,
            "net_exposure": net_after,
            "margin_used": margin_after,
        }
        out: dict[str, Any] = {k: str(v) for k, v in values.items()}
        out["of_equity"] = {k: _s(_pct(v, p.equity)) for k, v in values.items()}
        held = self._held()
        out["open_positions"] = len(p.positions) + (0 if held is not None else 1)
        return out


def assess(inputs: AssessmentInputs) -> AssessmentOutcome:
    """Assess one proposal. Pure and deterministic."""
    return _Assessment(inputs).run()


__all__ = ["ENGINE_VERSION", "NOT_AN_APPROVAL", "assess"]

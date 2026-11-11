"""The backtest account as the Phase 6B risk engine sees it.

:meth:`RiskBook.view` mirrors ``app.risk.state.evaluate`` on the in-memory ledger: the
ledger is marked to the replay's prices with the 6A valuation template (nothing posted),
exposure per position comes from the cost and valuation accounts, short collateral from the
larger of the configured minimum and the provider's requirement, the daily and weekly loss
from baselines chosen by ``app.risk.periods.choose_baseline`` and fixed once, drawdown from
``peak_equity``, the loss allowance from ``loss_state``, outstanding reservations as
``ReservedExposure``, and the breaker conditions from ``app.risk.breakers.conditions``.
Breaker transitions use ``app.risk.breakers.transitions`` (automatic trips and automatic
resets exactly as live). Manual breakers have no operator in a replay; the configured
:class:`~app.backtesting.enums.BreakerResetPolicy` is the simulated operator, and every
reset it makes is recorded with that actor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.accounting import chart
from app.accounting.money import ZERO, money_context
from app.accounting.state import LedgerState
from app.accounting.templates import PriceMark
from app.backtesting.enums import BreakerResetPolicy
from app.backtesting.ledger import SimulatedLedger
from app.portfolio.figures import figures
from app.risk import breakers as rules
from app.risk.config import RiskConfiguration
from app.risk.enums import (
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    MarkStatus,
    PeriodKind,
    TradeDirection,
)
from app.risk.models import HeldPosition, PortfolioView, ReservedExposure
from app.risk.periods import Baseline, Observation, choose_baseline, loss_state, peak_equity, period

SIMULATED_OPERATOR = "backtest:simulated-operator"
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class PositionMark:
    """What the replay knows about one open position at a decision time."""

    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    price: Decimal | None  # None: no fresh price (the mark is INCOMPLETE)
    status: str  # FRESH | STALE | MISSING
    margin_requirement_short: Decimal | None = None  # provider percentage (100 = 100%)
    sector: str | None = None
    cluster: str | None = None


@dataclass
class BreakerRecord:
    at: datetime
    breaker: BreakerType
    event: BreakerEvent
    actor: str
    reason: str
    measurements: dict[str, Any]


@dataclass
class Evaluated:
    view: PortfolioView
    conditions: list[rules.Condition]
    marked: LedgerState


def position_exposure(state: LedgerState, asset_id: uuid.UUID, currency: str) -> Decimal:
    """Marked exposure of a position (6B ``state._position_exposure``)."""
    pos = state.positions[asset_id]
    if pos.quantity > ZERO:
        return (
            state.balance(chart.LONG_COST, currency, asset_id).functional
            + state.balance(chart.LONG_VALUATION, currency, asset_id).functional
        )
    return -(
        state.balance(chart.SHORT_PROCEEDS, currency, asset_id).functional
        + state.balance(chart.SHORT_VALUATION, currency, asset_id).functional
    )


@dataclass
class RiskBook:
    account_id: uuid.UUID
    config: RiskConfiguration
    policy: BreakerResetPolicy
    functional: str = "USD"
    observations: list[Observation] = field(default_factory=list)
    funded_at: datetime | None = None
    funding: Decimal | None = None
    baselines: dict[tuple[PeriodKind, str], Baseline] = field(default_factory=dict)
    statuses: dict[BreakerType, BreakerStatus] = field(default_factory=dict)
    tripped_at: dict[BreakerType, datetime] = field(default_factory=dict)
    history: list[BreakerRecord] = field(default_factory=list)
    reconciled_sequence: int | None = None
    reconciliation_status: str | None = None

    def observe(self, at: datetime, equity: Decimal, capital: Decimal) -> None:
        self.observations.append(
            Observation(at, equity, capital, "BACKTEST_SNAPSHOT", at.isoformat())
        )

    def _baseline(self, kind: PeriodKind, at: datetime, current: Observation) -> Baseline | None:
        p = period(kind, at)
        key = (kind, p.label)
        fixed = self.baselines.get(key)
        if fixed is not None:
            return fixed
        chosen = choose_baseline(
            p,
            [o for o in self.observations if o.observed_at <= at],
            funded_at=self.funded_at,
            funding=self.funding,
            current=current,
        )
        if chosen is not None:
            self.baselines[key] = chosen
        return chosen

    def evaluate(
        self,
        ledger: SimulatedLedger,
        marks: dict[uuid.UUID, PositionMark],
        reservations: list[ReservedExposure],
        at: datetime,
    ) -> Evaluated:
        limits = self.config.hard_limits
        issues: list[dict[str, Any]] = []
        price_marks: list[PriceMark] = []
        state = ledger.state
        for asset_id, pos in sorted(state.positions.items(), key=lambda kv: str(kv[0])):
            if pos.quantity == ZERO:
                continue
            m = marks.get(asset_id)
            if m is None or m.price is None or m.status != "FRESH":
                issues.append(
                    {
                        "kind": f"PRICE_{'MISSING' if m is None or m.price is None else m.status}",
                        "asset_id": str(asset_id),
                        "symbol": None if m is None else m.symbol,
                    }
                )
                continue
            price_marks.append(PriceMark(asset_id=asset_id, price=m.price, currency=pos.currency))
        marked = ledger.marked(price_marks, at)
        totals = figures(marked)
        if issues:
            mark_status = (
                MarkStatus.INCOMPLETE
                if any(str(i["kind"]).endswith("MISSING") for i in issues)
                else MarkStatus.STALE
            )
        else:
            mark_status = MarkStatus.COMPLETE
        positions: list[HeldPosition] = []
        for asset_id, pos in sorted(marked.positions.items(), key=lambda kv: str(kv[0])):
            if pos.quantity == ZERO:
                continue
            m = marks.get(asset_id)
            exposure = abs(position_exposure(marked, asset_id, pos.currency))
            short = pos.quantity < ZERO
            rate = ZERO
            if short:
                provider = (
                    None
                    if m is None
                    or m.margin_requirement_short is None
                    or m.margin_requirement_short < 0
                    else m.margin_requirement_short / HUNDRED
                )
                rate = max(limits.min_short_margin_rate, provider or ZERO)
            with money_context():
                margin = rate * exposure
            positions.append(
                HeldPosition(
                    asset_id=asset_id,
                    symbol="?" if m is None else m.symbol,
                    asset_class="EQUITY" if m is None else m.asset_class,
                    currency=pos.currency,
                    direction=TradeDirection.SHORT if short else TradeDirection.LONG,
                    quantity=pos.quantity,
                    exposure=exposure,
                    price_status="MISSING" if m is None else m.status,
                    margin_required=margin,
                    sector=None if m is None else m.sector,
                    cluster=None if m is None else m.cluster,
                )
            )
        long_x = sum((p.exposure for p in positions if p.quantity > 0), ZERO)
        short_x = sum((p.exposure for p in positions if p.quantity < 0), ZERO)
        margin_used = sum((p.margin_required for p in positions), ZERO)
        equity = totals.equity
        daily_loss = weekly_loss = drawdown = peak = budget = None
        daily_base = weekly_base = None
        if mark_status is MarkStatus.COMPLETE:
            current = Observation(at, equity, totals.contributed_capital, "CURRENT", "current")
            daily = self._baseline(PeriodKind.DAILY, at, current)
            weekly = self._baseline(PeriodKind.WEEKLY, at, current)
            if daily is not None and weekly is not None:
                peak = peak_equity(
                    [o for o in self.observations if o.observed_at <= at],
                    equity,
                    totals.contributed_capital,
                )
                ls = loss_state(
                    equity=equity,
                    contributed_capital=totals.contributed_capital,
                    daily=daily,
                    weekly=weekly,
                    peak=peak,
                    daily_limit=limits.daily_loss_limit_pct,
                    weekly_limit=limits.weekly_loss_limit_pct,
                    drawdown_limit=limits.max_drawdown_pct,
                )
                daily_loss, weekly_loss, drawdown = ls.daily_loss, ls.weekly_loss, ls.drawdown
                daily_base, weekly_base = ls.daily_baseline, ls.weekly_baseline
                budget = ls.available_risk_budget
        current_recon = (
            self.reconciliation_status is not None and self.reconciled_sequence == ledger.sequence
        )
        found = rules.conditions(
            rules.StateFigures(
                mark_status=mark_status,
                mark_issues=issues,
                equity=equity,
                gross_exposure=long_x + short_x,
                net_exposure=long_x - short_x,
                short_exposure=short_x,
                margin_used=margin_used,
                open_positions=len(positions),
                daily_loss=daily_loss,
                daily_baseline=daily_base,
                weekly_loss=weekly_loss,
                weekly_baseline=weekly_base,
                drawdown=drawdown,
                peak_equity=peak,
                reconciliation_status=self.reconciliation_status,
            ),
            limits,
        )
        pending = sum((r.exposure for r in reservations), ZERO)
        view = PortfolioView(
            account_id=self.account_id,
            functional_currency=self.functional,
            ledger_sequence=ledger.sequence,
            mark_status=mark_status,
            mark_issues=issues,
            equity=equity,
            contributed_capital=totals.contributed_capital,
            cash=totals.cash,
            available_capital=totals.available_capital - margin_used,
            long_exposure=long_x,
            short_exposure=short_x,
            gross_exposure=long_x + short_x,
            net_exposure=long_x - short_x,
            margin_used=margin_used,
            positions=positions,
            daily_loss=daily_loss,
            daily_baseline=daily_base,
            weekly_loss=weekly_loss,
            weekly_baseline=weekly_base,
            peak_equity=peak,
            drawdown=drawdown,
            available_risk_budget=budget,
            pending_exposure=pending,
            pending_source=(
                f"{len(reservations)} active risk reservation(s)"
                if reservations
                else "none: no active risk reservation"
            ),
            reservations=reservations,
            active_breakers=self.active(),
            reconciliation_status=self.reconciliation_status,
            reconciliation_current=current_recon,
        )
        return Evaluated(view, found, marked)

    def active(self) -> list[BreakerType]:
        return [b for b, s in sorted(self.statuses.items()) if s is BreakerStatus.TRIPPED]

    def monitor(self, found: list[rules.Condition], at: datetime) -> list[BreakerRecord]:
        """Apply the 6B transitions (trips; automatic resets), as the risk monitor does."""
        out: list[BreakerRecord] = []
        for t in rules.transitions(found, self.statuses):
            record = BreakerRecord(
                at,
                t.breaker,
                t.event,
                "system:risk-monitor",
                t.condition.message
                if t.event is BreakerEvent.TRIPPED
                else f"automatic recovery: {t.condition.message}",
                t.condition.measurements(),
            )
            self._record(record)
            out.append(record)
        return out

    def simulated_operator(
        self, found: list[rules.Condition], at: datetime, session_open: datetime
    ) -> list[BreakerRecord]:
        """Reset tripped manual breakers whose condition is clear, if the policy allows and
        the trip happened before this session (a reset is never same-session)."""
        if self.policy is BreakerResetPolicy.NEVER:
            return []
        by_type = {c.breaker: c for c in found}
        out: list[BreakerRecord] = []
        for breaker in self.active():
            if rules.RECOVERY[breaker].value != "OPERATOR_RESET":
                continue
            if self.tripped_at.get(breaker, at) >= session_open:
                continue
            allowed, why = rules.reset_allowed(breaker, by_type.get(breaker))
            if not allowed or breaker in (BreakerType.KILL_SWITCH, BreakerType.EXECUTION_SAFETY):
                continue
            record = BreakerRecord(
                at,
                breaker,
                BreakerEvent.RESET,
                SIMULATED_OPERATOR,
                f"simulated operator reset ({self.policy.value}): {why}",
                {} if breaker not in by_type else by_type[breaker].measurements(),
            )
            self._record(record)
            out.append(record)
        return out

    def _record(self, record: BreakerRecord) -> None:
        self.history.append(record)
        if record.event is BreakerEvent.TRIPPED:
            self.statuses[record.breaker] = BreakerStatus.TRIPPED
            self.tripped_at[record.breaker] = record.at
        else:
            self.statuses[record.breaker] = BreakerStatus.CLEAR
            self.tripped_at.pop(record.breaker, None)


__all__ = [
    "SIMULATED_OPERATOR",
    "BreakerRecord",
    "Evaluated",
    "PositionMark",
    "RiskBook",
    "position_exposure",
]

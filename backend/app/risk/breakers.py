"""Circuit-breaker conditions and transitions (spec 14.5; decision D-141). Pure.

Every breaker blocks new entries while TRIPPED (assessments return RISK_HALTED). None of them
liquidates or closes anything; position monitoring continues, and risk-reducing operations
remain permitted for the future execution phases (the breaker records say so).

* DAILY_LOSS: loss since the daily baseline >= daily limit x baseline. Operator reset,
  once clear.
* WEEKLY_LOSS: loss since the weekly baseline >= weekly limit x baseline. Operator reset,
  once clear.
* MAX_DRAWDOWN: peak - equity >= drawdown limit x peak. Operator reset, once clear.
* EXCESSIVE_EXPOSURE: gross, leverage, net, short exposure, margin usage or the position
  count above its limit, or equity <= 0. Operator reset, once clear.
* DATA_QUALITY: an open position or foreign currency cannot be marked to a fresh price or
  rate. Automatic RESET (recorded) once every input is fresh again.
* RECONCILIATION_FAILURE: the latest accounting reconciliation FAILED. Operator reset, once
  a later run passes.
* EXECUTION_SAFETY: reported through ``report_execution_safety`` (no producer before
  Phase 7). Operator reset.
* KILL_SWITCH: engaged by an operator. Operator release.

Nothing is reset silently: every transition is an append-only event with its actor, reason
and measurements, and the database refuses a system reset of anything but DATA_QUALITY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.accounting.money import ZERO, money_context
from app.risk.config import HardLimits
from app.risk.enums import BreakerEvent, BreakerStatus, BreakerType, MarkStatus, RecoveryMode

RECOVERY: dict[BreakerType, RecoveryMode] = {
    BreakerType.DAILY_LOSS: RecoveryMode.OPERATOR_RESET,
    BreakerType.WEEKLY_LOSS: RecoveryMode.OPERATOR_RESET,
    BreakerType.MAX_DRAWDOWN: RecoveryMode.OPERATOR_RESET,
    BreakerType.EXCESSIVE_EXPOSURE: RecoveryMode.OPERATOR_RESET,
    BreakerType.DATA_QUALITY: RecoveryMode.AUTO_WHEN_CLEAR,
    BreakerType.RECONCILIATION_FAILURE: RecoveryMode.OPERATOR_RESET,
    BreakerType.EXECUTION_SAFETY: RecoveryMode.OPERATOR_RESET,
    BreakerType.KILL_SWITCH: RecoveryMode.OPERATOR_RESET,
}
# Breakers the monitor evaluates from portfolio state; the others are set by an actor.
EVALUATED = (
    BreakerType.DAILY_LOSS,
    BreakerType.WEEKLY_LOSS,
    BreakerType.MAX_DRAWDOWN,
    BreakerType.EXCESSIVE_EXPOSURE,
    BreakerType.DATA_QUALITY,
    BreakerType.RECONCILIATION_FAILURE,
)
RESPONSE = {
    "blocks": "new entries (assessments return RISK_HALTED)",
    "continues": "monitoring of existing positions",
    "permits": "risk-reducing operations in the execution phases",
    "liquidates": "nothing: Atlas never closes positions automatically",
}


@dataclass(frozen=True)
class Condition:
    breaker: BreakerType
    # True: breached; False: clear; None: could not be evaluated (data missing).
    breached: bool | None
    message: str
    measured: Decimal | None = None
    limit: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def measurements(self) -> dict[str, Any]:
        return {
            "breached": self.breached,
            "message": self.message,
            "measured": None if self.measured is None else str(self.measured),
            "limit": None if self.limit is None else str(self.limit),
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.details.items()},
        }


@dataclass(frozen=True)
class StateFigures:
    """What the conditions read (functional currency)."""

    mark_status: MarkStatus
    mark_issues: list[dict[str, Any]]
    equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    short_exposure: Decimal
    margin_used: Decimal
    open_positions: int
    daily_loss: Decimal | None
    daily_baseline: Decimal | None
    weekly_loss: Decimal | None
    weekly_baseline: Decimal | None
    drawdown: Decimal | None
    peak_equity: Decimal | None
    reconciliation_status: str | None
    reconciliation_run_id: str | None = None


def _loss(
    breaker: BreakerType, loss: Decimal | None, base: Decimal | None, limit_pct: Decimal, what: str
) -> Condition:
    if loss is None or base is None:
        return Condition(breaker, None, f"{what} could not be measured")
    with money_context():
        limit = limit_pct * base
    breached = loss >= limit
    return Condition(
        breaker,
        breached,
        f"{what} {loss} vs limit {limit} ({limit_pct} of {base})",
        loss,
        limit,
        {"limit_pct": limit_pct, "base": base},
    )


def conditions(s: StateFigures, limits: HardLimits) -> list[Condition]:
    out = [
        _loss(
            BreakerType.DAILY_LOSS,
            s.daily_loss,
            s.daily_baseline,
            limits.daily_loss_limit_pct,
            "daily loss",
        ),
        _loss(
            BreakerType.WEEKLY_LOSS,
            s.weekly_loss,
            s.weekly_baseline,
            limits.weekly_loss_limit_pct,
            "weekly loss",
        ),
        _loss(
            BreakerType.MAX_DRAWDOWN,
            s.drawdown,
            s.peak_equity,
            limits.max_drawdown_pct,
            "drawdown",
        ),
    ]
    if s.mark_status is not MarkStatus.COMPLETE:
        out.append(
            Condition(
                BreakerType.EXCESSIVE_EXPOSURE, None, "exposure cannot be measured (not marked)"
            )
        )
    else:
        e = s.equity
        breaches: list[str] = []
        with money_context():
            checks = [
                ("gross exposure", s.gross_exposure, limits.max_gross_exposure_pct * e),
                ("leverage", s.gross_exposure, limits.max_leverage * e),
                ("net exposure", abs(s.net_exposure), limits.max_net_exposure_pct * e),
                ("short exposure", s.short_exposure, limits.max_short_exposure_pct * e),
                ("margin usage", s.margin_used, limits.max_margin_usage_pct * e),
            ]
        if e <= ZERO:
            breaches.append(f"equity {e} is not positive")
        for name, used, limit in checks:
            if used > limit:
                breaches.append(f"{name} {used} > {limit}")
        if s.open_positions > limits.max_open_positions:
            breaches.append(f"{s.open_positions} positions > {limits.max_open_positions}")
        out.append(
            Condition(
                BreakerType.EXCESSIVE_EXPOSURE,
                bool(breaches),
                "; ".join(breaches) if breaches else "exposure within every limit",
                s.gross_exposure,
                None,
                {"breaches": breaches},
            )
        )
    out.append(
        Condition(
            BreakerType.DATA_QUALITY,
            s.mark_status is not MarkStatus.COMPLETE,
            "every open position and currency marked to fresh inputs"
            if s.mark_status is MarkStatus.COMPLETE
            else f"portfolio not marked ({s.mark_status.value})",
            details={"issues": s.mark_issues},
        )
    )
    if s.reconciliation_status is None:
        out.append(
            Condition(BreakerType.RECONCILIATION_FAILURE, None, "no reconciliation run exists")
        )
    else:
        failed = s.reconciliation_status != "PASSED"
        out.append(
            Condition(
                BreakerType.RECONCILIATION_FAILURE,
                failed,
                f"latest reconciliation {s.reconciliation_status}",
                details={"run_id": s.reconciliation_run_id},
            )
        )
    return out


@dataclass(frozen=True)
class Transition:
    breaker: BreakerType
    event: BreakerEvent
    condition: Condition


def transitions(
    found: list[Condition], status: dict[BreakerType, BreakerStatus]
) -> list[Transition]:
    """Trips for breached conditions of CLEAR breakers; automatic resets only for
    AUTO_WHEN_CLEAR breakers whose condition is clear. Unknown conditions change nothing."""
    out: list[Transition] = []
    for c in found:
        current = status.get(c.breaker, BreakerStatus.CLEAR)
        if c.breached is True and current is BreakerStatus.CLEAR:
            out.append(Transition(c.breaker, BreakerEvent.TRIPPED, c))
        elif (
            c.breached is False
            and current is BreakerStatus.TRIPPED
            and RECOVERY[c.breaker] is RecoveryMode.AUTO_WHEN_CLEAR
        ):
            out.append(Transition(c.breaker, BreakerEvent.RESET, c))
    return out


def reset_allowed(breaker: BreakerType, condition: Condition | None) -> tuple[bool, str]:
    """Whether an operator may reset ``breaker`` now (a breached condition must clear first)."""
    if breaker in (BreakerType.KILL_SWITCH, BreakerType.EXECUTION_SAFETY):
        return True, "operator reset"
    if condition is None or condition.breached is None:
        return False, "the condition cannot be evaluated now (missing data): not reset"
    if condition.breached:
        return False, f"the condition is still breached: {condition.message}"
    return True, f"condition clear: {condition.message}"


__all__ = [
    "EVALUATED",
    "RECOVERY",
    "RESPONSE",
    "Condition",
    "StateFigures",
    "Transition",
    "conditions",
    "reset_allowed",
    "transitions",
]

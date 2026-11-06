"""Loss periods, baselines, drawdown and loss allowance (spec 14.5; decision D-143). Pure.

Timezone policy: one portfolio-wide calendar, the NYSE (XNYS, America/New_York, holidays and
early closes from ``app.data_engine.calendar``), the same calendar Phase 6A uses for price
freshness. All stored times are UTC.

* **Trading day.** A risk day ends at a session's regular close (16:00 ET, 13:00 ET on early
  closes). The daily period containing time *t* starts at the latest regular close strictly
  before *t* and ends at the next one: a weekend or holiday belongs to the next session's day.
* **Trading week.** The weekly period of *t* is the ISO week (Monday-Sunday, New York time)
  of *t*'s risk day; it starts at the regular close of the last session before that week.
* **Start-of-period equity (baseline).** For the boundary *B* (a regular close), the first
  of these that exists, fixed once per period and stored:

  1. the latest COMPLETE equity observation during the session that closed at *B* (between
     its open and *B*), else the earliest one after *B* but before the next session opens
     (prices do not move while the market is closed): OBSERVED_AT_BOUNDARY;
  2. the account was funded after *B*: its opening funding: FUNDED_IN_PERIOD;
  3. the first COMPLETE observation inside the period: FIRST_OBSERVATION_IN_PERIOD. Losses
     between *B* and that observation are then not captured; the state says so.

* **Loss.** Period P/L = change in investment return (equity - contributed capital) since the
  baseline, so it includes realized and unrealized P/L, FX, commissions, fees, borrow and
  funding costs, and never counts funding as a gain. Loss = -P/L. A loss limit is breached
  when loss >= limit x baseline equity.
* **Drawdown.** Peak equity = the highest investment return of any COMPLETE observation (and
  zero at funding), plus contributed capital. Drawdown = peak - equity; breached when
  drawdown >= limit x peak.
* **Loss allowance** (available risk budget) = the smallest remaining room before the daily,
  weekly or drawdown limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.accounting.money import ZERO, money_context
from app.asset_discovery.sessions import (
    CALENDARS,
    SessionDay,
    local_date,
    next_trading_day,
    previous_trading_day,
    trading_day,
)
from app.risk.enums import BaselineBasis, PeriodKind

CALENDAR = CALENDARS["XNYS"]
TIMEZONE_POLICY = (
    "XNYS calendar (America/New_York) for the whole portfolio: a risk day ends at the regular "
    "close; a risk week is the ISO week of the risk day; times are stored in UTC"
)


def risk_day(at: datetime) -> SessionDay:
    """The trading session whose daily period contains ``at`` (it closes at or after ``at``)."""
    today = trading_day(CALENDAR, local_date(CALENDAR, at))
    if today is not None and at <= today.regular_close:
        return today
    return next_trading_day(CALENDAR, local_date(CALENDAR, at))


def session_before(day: date) -> SessionDay:
    return previous_trading_day(CALENDAR, day)


@dataclass(frozen=True)
class Period:
    kind: PeriodKind
    label: str
    start: datetime  # the boundary B (a regular close), exclusive
    end: datetime  # the closing boundary, inclusive
    boundary_session: SessionDay  # the session that closed at B
    next_open: datetime  # when the market next opens after B


def period(kind: PeriodKind, at: datetime) -> Period:
    day = risk_day(at)
    if kind is PeriodKind.DAILY:
        before = session_before(day.day)
        label = day.day.isoformat()
        end = day.regular_close
    else:
        monday = day.day - timedelta(days=day.day.weekday())
        before = session_before(monday)
        iso = day.day.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
        last = day
        while True:
            following = next_trading_day(CALENDAR, last.day)
            if following.day.isocalendar()[:2] != iso[:2]:
                break
            last = following
        end = last.regular_close
    following_session = next_trading_day(CALENDAR, before.day)
    return Period(
        kind=kind,
        label=label,
        start=before.regular_close,
        end=end,
        boundary_session=before,
        next_open=following_session.regular_open,
    )


@dataclass(frozen=True)
class Observation:
    """A COMPLETE equity observation (risk snapshot or valuation checkpoint)."""

    observed_at: datetime
    equity: Decimal
    contributed_capital: Decimal
    source_kind: str
    source_id: str

    @property
    def investment_return(self) -> Decimal:
        return self.equity - self.contributed_capital


@dataclass(frozen=True)
class Baseline:
    kind: PeriodKind
    label: str
    period_start: datetime
    equity: Decimal
    contributed_capital: Decimal
    basis: BaselineBasis
    observed_at: datetime
    source_kind: str
    source_id: str

    @property
    def investment_return(self) -> Decimal:
        return self.equity - self.contributed_capital


def choose_baseline(
    p: Period,
    observations: list[Observation],
    *,
    funded_at: datetime | None,
    funding: Decimal | None,
    current: Observation | None,
) -> Baseline | None:
    """The start-of-period equity by the documented rule, or None when nothing qualifies."""

    def make(obs: Observation, basis: BaselineBasis) -> Baseline:
        return Baseline(
            p.kind,
            p.label,
            p.start,
            obs.equity,
            obs.contributed_capital,
            basis,
            obs.observed_at,
            obs.source_kind,
            obs.source_id,
        )

    during = [
        o for o in observations if p.boundary_session.regular_open <= o.observed_at <= p.start
    ]
    if during:
        return make(max(during, key=lambda o: o.observed_at), BaselineBasis.OBSERVED_AT_BOUNDARY)
    after_close = [o for o in observations if p.start < o.observed_at < p.next_open]
    if after_close:
        return make(
            min(after_close, key=lambda o: o.observed_at), BaselineBasis.OBSERVED_AT_BOUNDARY
        )
    if funded_at is not None and funding is not None and funded_at > p.start:
        return Baseline(
            p.kind,
            p.label,
            p.start,
            funding,
            funding,
            BaselineBasis.FUNDED_IN_PERIOD,
            funded_at,
            "OPENING_FUNDING",
            "opening_funding",
        )
    inside = [o for o in observations if o.observed_at > p.start]
    if current is not None and current.observed_at > p.start:
        inside.append(current)
    if inside:
        return make(
            min(inside, key=lambda o: o.observed_at), BaselineBasis.FIRST_OBSERVATION_IN_PERIOD
        )
    return None


def period_loss(baseline: Baseline, equity: Decimal, contributed_capital: Decimal) -> Decimal:
    """Loss since the baseline (positive = loss), from the change in investment return."""
    return baseline.investment_return - (equity - contributed_capital)


def peak_equity(
    observations: list[Observation], equity: Decimal, contributed_capital: Decimal
) -> Decimal:
    """Highest investment return observed (zero at funding, the current one included),
    expressed at today's contributed capital."""
    best = max([ZERO, equity - contributed_capital, *(o.investment_return for o in observations)])
    return best + contributed_capital


@dataclass(frozen=True)
class LossState:
    daily_loss: Decimal
    daily_baseline: Decimal
    weekly_loss: Decimal
    weekly_baseline: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    daily_room: Decimal
    weekly_room: Decimal
    drawdown_room: Decimal

    @property
    def available_risk_budget(self) -> Decimal:
        return max(min(self.daily_room, self.weekly_room, self.drawdown_room), ZERO)


def loss_state(
    *,
    equity: Decimal,
    contributed_capital: Decimal,
    daily: Baseline,
    weekly: Baseline,
    peak: Decimal,
    daily_limit: Decimal,
    weekly_limit: Decimal,
    drawdown_limit: Decimal,
) -> LossState:
    with money_context():
        daily_loss = period_loss(daily, equity, contributed_capital)
        weekly_loss = period_loss(weekly, equity, contributed_capital)
        drawdown = peak - equity
        return LossState(
            daily_loss=daily_loss,
            daily_baseline=daily.equity,
            weekly_loss=weekly_loss,
            weekly_baseline=weekly.equity,
            peak_equity=peak,
            drawdown=drawdown,
            daily_room=daily_limit * daily.equity - daily_loss,
            weekly_room=weekly_limit * weekly.equity - weekly_loss,
            drawdown_room=drawdown_limit * peak - drawdown,
        )


__all__ = [
    "CALENDAR",
    "TIMEZONE_POLICY",
    "Baseline",
    "LossState",
    "Observation",
    "Period",
    "choose_baseline",
    "loss_state",
    "peak_equity",
    "period",
    "period_loss",
    "risk_day",
]

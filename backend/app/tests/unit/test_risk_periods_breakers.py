"""Phase 6B loss periods, baselines, drawdown and circuit-breaker rules (pure)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import pytest

from app.risk.breakers import (
    RECOVERY,
    Condition,
    StateFigures,
    conditions,
    reset_allowed,
    transitions,
)
from app.risk.config import HardLimits
from app.risk.enums import (
    BaselineBasis,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    MarkStatus,
    PeriodKind,
    RecoveryMode,
)
from app.risk.periods import (
    Observation,
    choose_baseline,
    loss_state,
    peak_equity,
    period,
    period_loss,
)

DAILY, WEEKLY = PeriodKind.DAILY, PeriodKind.WEEKLY


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def obs(at: datetime, equity: str, capital: str = "100000") -> Observation:
    return Observation(at, D(equity), D(capital), "TEST", at.isoformat())


# ------------------------------------------------------------------ calendar boundaries
def test_daily_period_during_a_session():
    # Tuesday 3 Nov 2026 10:00 ET (EST since 1 Nov): the period began at Monday's close.
    p = period(DAILY, utc(2026, 11, 3, 15, 0))
    assert p.label == "2026-11-03"
    assert p.start == utc(2026, 11, 2, 21, 0)
    assert p.end == utc(2026, 11, 3, 21, 0)
    assert p.next_open == utc(2026, 11, 3, 14, 30)


def test_the_close_belongs_to_its_day_and_after_hours_to_the_next():
    assert period(DAILY, utc(2026, 11, 3, 21, 0)).label == "2026-11-03"
    after = period(DAILY, utc(2026, 11, 3, 21, 0, 1))
    assert after.label == "2026-11-04"
    assert after.start == utc(2026, 11, 3, 21, 0)


def test_weekend_belongs_to_the_next_session():
    p = period(DAILY, utc(2026, 11, 7, 12, 0))  # Saturday
    assert p.label == "2026-11-09"
    assert p.start == utc(2026, 11, 6, 21, 0)  # Friday's close


def test_holiday_and_early_close():
    # Thanksgiving Thursday 26 Nov 2026 is closed; Friday closes early at 13:00 ET.
    p = period(DAILY, utc(2026, 11, 26, 12, 0))
    assert p.label == "2026-11-27"
    assert p.start == utc(2026, 11, 25, 21, 0)
    assert p.end == utc(2026, 11, 27, 18, 0)


def test_daylight_saving_boundary():
    # Friday 30 Oct 2026 closes at 16:00 EDT = 20:00 UTC; Monday 2 Nov's period starts there.
    p = period(DAILY, utc(2026, 11, 2, 15, 0))
    assert p.start == utc(2026, 10, 30, 20, 0)


def test_weekly_period():
    p = period(WEEKLY, utc(2026, 11, 4, 15, 0))
    assert p.label == "2026-W45"
    assert p.start == utc(2026, 10, 30, 20, 0)  # Friday before, EDT close
    assert p.end == utc(2026, 11, 6, 21, 0)
    # Friday after the close belongs to the next week.
    assert period(WEEKLY, utc(2026, 11, 6, 22, 0)).label == "2026-W46"
    # Thanksgiving week ends at Friday's early close.
    assert period(WEEKLY, utc(2026, 11, 24, 15, 0)).end == utc(2026, 11, 27, 18, 0)


# ------------------------------------------------------------------ baselines
def test_baseline_prefers_an_observation_during_the_boundary_session():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))  # starts Tue 21:00
    chosen = choose_baseline(
        p,
        [obs(utc(2026, 11, 3, 15, 0), "99000"), obs(utc(2026, 11, 3, 20, 0), "99500")],
        funded_at=None,
        funding=None,
        current=None,
    )
    assert chosen is not None
    assert chosen.basis is BaselineBasis.OBSERVED_AT_BOUNDARY
    assert chosen.equity == D("99500")


def test_baseline_uses_an_after_close_observation_before_the_next_open():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))
    chosen = choose_baseline(
        p,
        [obs(utc(2026, 11, 3, 22, 30), "99700"), obs(utc(2026, 11, 4, 14, 45), "98000")],
        funded_at=None,
        funding=None,
        current=None,
    )
    assert chosen is not None
    assert chosen.basis is BaselineBasis.OBSERVED_AT_BOUNDARY
    assert chosen.equity == D("99700")


def test_baseline_ignores_older_sessions_and_falls_back_explicitly():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))
    old = obs(utc(2026, 11, 2, 18, 0), "120000")  # Monday: not the boundary session
    late = obs(utc(2026, 11, 4, 14, 45), "98000")
    chosen = choose_baseline(p, [old, late], funded_at=None, funding=None, current=None)
    assert chosen is not None
    assert chosen.basis is BaselineBasis.FIRST_OBSERVATION_IN_PERIOD
    assert chosen.equity == D("98000")
    assert choose_baseline(p, [old], funded_at=None, funding=None, current=None) is None


def test_baseline_is_the_funding_when_funded_in_the_period():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))
    chosen = choose_baseline(
        p, [], funded_at=utc(2026, 11, 3, 22, 0), funding=D("100000"), current=None
    )
    assert chosen is not None
    assert chosen.basis is BaselineBasis.FUNDED_IN_PERIOD
    assert chosen.equity == D("100000")
    assert chosen.investment_return == 0


# ------------------------------------------------------------------ loss and drawdown
def test_loss_includes_everything_but_never_counts_funding_as_a_gain():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))
    base = choose_baseline(
        p, [obs(utc(2026, 11, 3, 20, 0), "100000")], funded_at=None, funding=None, current=None
    )
    assert base is not None
    assert period_loss(base, D("98000"), D("100000")) == D("2000")
    # Extra capital is not a gain: 150,000 equity with 150,000 capital is a zero P/L.
    assert period_loss(base, D("150000"), D("150000")) == 0
    assert period_loss(base, D("101000"), D("100000")) == D("-1000")


def test_peak_equity_and_drawdown():
    observations = [obs(utc(2026, 11, 2, 15), "110000"), obs(utc(2026, 11, 2, 16), "105000")]
    assert peak_equity(observations, D("99000"), D("100000")) == D("110000")
    assert peak_equity([], D("95000"), D("100000")) == D("100000")  # funding is the floor
    assert peak_equity([], D("101000"), D("100000")) == D("101000")


def test_loss_state_and_allowance():
    p = period(DAILY, utc(2026, 11, 4, 15, 0))
    daily = choose_baseline(
        p, [obs(utc(2026, 11, 3, 20), "100000")], funded_at=None, funding=None, current=None
    )
    weekly = choose_baseline(
        period(WEEKLY, utc(2026, 11, 4, 15, 0)),
        [obs(utc(2026, 10, 30, 19), "102000")],
        funded_at=None,
        funding=None,
        current=None,
    )
    assert daily is not None
    assert weekly is not None
    s = loss_state(
        equity=D("99000"),
        contributed_capital=D("100000"),
        daily=daily,
        weekly=weekly,
        peak=D("105000"),
        daily_limit=D("0.02"),
        weekly_limit=D("0.05"),
        drawdown_limit=D("0.10"),
    )
    assert s.daily_loss == D("1000")
    assert s.weekly_loss == D("3000")
    assert s.drawdown == D("6000")
    assert s.daily_room == D("1000")  # 2,000 - 1,000
    assert s.weekly_room == D("2100")  # 5,100 - 3,000
    assert s.drawdown_room == D("4500")  # 10,500 - 6,000
    assert s.available_risk_budget == D("1000")


# ------------------------------------------------------------------ breaker conditions
def figures(**overrides: Any) -> StateFigures:
    values: dict[str, Any] = {
        "mark_status": MarkStatus.COMPLETE,
        "mark_issues": [],
        "equity": D("100000"),
        "gross_exposure": D("50000"),
        "net_exposure": D("50000"),
        "short_exposure": D("0"),
        "margin_used": D("0"),
        "open_positions": 3,
        "daily_loss": D("0"),
        "daily_baseline": D("100000"),
        "weekly_loss": D("0"),
        "weekly_baseline": D("100000"),
        "drawdown": D("0"),
        "peak_equity": D("100000"),
        "reconciliation_status": "PASSED",
    }
    values.update(overrides)
    return StateFigures(**values)


def by_type(found: list[Condition]) -> dict[BreakerType, Condition]:
    return {c.breaker: c for c in found}


@pytest.mark.parametrize(
    ("field", "value", "breaker", "breached"),
    [
        ("daily_loss", "1999.99", BreakerType.DAILY_LOSS, False),
        ("daily_loss", "2000", BreakerType.DAILY_LOSS, True),  # reaching the limit trips
        ("weekly_loss", "4999.99", BreakerType.WEEKLY_LOSS, False),
        ("weekly_loss", "5000", BreakerType.WEEKLY_LOSS, True),
        ("drawdown", "9999.99", BreakerType.MAX_DRAWDOWN, False),
        ("drawdown", "10000", BreakerType.MAX_DRAWDOWN, True),
        ("gross_exposure", "100000", BreakerType.EXCESSIVE_EXPOSURE, False),  # at the limit
        ("gross_exposure", "100000.01", BreakerType.EXCESSIVE_EXPOSURE, True),
        ("short_exposure", "30000.01", BreakerType.EXCESSIVE_EXPOSURE, True),
        ("margin_used", "50000.01", BreakerType.EXCESSIVE_EXPOSURE, True),
        ("net_exposure", "-100000.01", BreakerType.EXCESSIVE_EXPOSURE, True),
    ],
)
def test_breaker_thresholds(field, value, breaker, breached):
    found = by_type(conditions(figures(**{field: D(value)}), HardLimits()))
    assert found[breaker].breached is breached


def test_position_count_and_equity_exposure_conditions():
    assert by_type(conditions(figures(open_positions=9), HardLimits()))[
        BreakerType.EXCESSIVE_EXPOSURE
    ].breached
    assert by_type(conditions(figures(equity=D("0")), HardLimits()))[
        BreakerType.EXCESSIVE_EXPOSURE
    ].breached


def test_unmarked_portfolio_trips_data_quality_and_leaves_others_unknown():
    found = by_type(
        conditions(
            figures(
                mark_status=MarkStatus.STALE,
                daily_loss=None,
                daily_baseline=None,
                drawdown=None,
                peak_equity=None,
            ),
            HardLimits(),
        )
    )
    assert found[BreakerType.DATA_QUALITY].breached is True
    assert found[BreakerType.DAILY_LOSS].breached is None
    assert found[BreakerType.MAX_DRAWDOWN].breached is None
    assert found[BreakerType.EXCESSIVE_EXPOSURE].breached is None


def test_reconciliation_conditions():
    assert by_type(conditions(figures(reconciliation_status="FAILED"), HardLimits()))[
        BreakerType.RECONCILIATION_FAILURE
    ].breached
    assert (
        by_type(conditions(figures(reconciliation_status=None), HardLimits()))[
            BreakerType.RECONCILIATION_FAILURE
        ].breached
        is None
    )


# ------------------------------------------------------------------ transitions
def clear() -> dict[BreakerType, BreakerStatus]:
    return dict.fromkeys(BreakerType, BreakerStatus.CLEAR)


def test_breached_condition_trips_once():
    found = conditions(figures(daily_loss=D("2500")), HardLimits())
    first = transitions(found, clear())
    assert [(t.breaker, t.event) for t in first] == [(BreakerType.DAILY_LOSS, BreakerEvent.TRIPPED)]
    already = clear() | {BreakerType.DAILY_LOSS: BreakerStatus.TRIPPED}
    assert transitions(found, already) == []


def test_loss_breakers_never_reset_automatically():
    tripped = clear() | {
        BreakerType.DAILY_LOSS: BreakerStatus.TRIPPED,
        BreakerType.MAX_DRAWDOWN: BreakerStatus.TRIPPED,
        BreakerType.EXCESSIVE_EXPOSURE: BreakerStatus.TRIPPED,
        BreakerType.RECONCILIATION_FAILURE: BreakerStatus.TRIPPED,
    }
    assert transitions(conditions(figures(), HardLimits()), tripped) == []


def test_data_quality_recovers_automatically_and_visibly():
    tripped = clear() | {BreakerType.DATA_QUALITY: BreakerStatus.TRIPPED}
    out = transitions(conditions(figures(), HardLimits()), tripped)
    assert [(t.breaker, t.event) for t in out] == [(BreakerType.DATA_QUALITY, BreakerEvent.RESET)]


def test_unknown_conditions_change_nothing():
    tripped = clear() | {BreakerType.DAILY_LOSS: BreakerStatus.TRIPPED}
    unknown = conditions(figures(daily_loss=None, daily_baseline=None), HardLimits())
    assert transitions(unknown, tripped) == []


def test_recovery_modes_are_fixed():
    assert RECOVERY[BreakerType.DATA_QUALITY] is RecoveryMode.AUTO_WHEN_CLEAR
    assert {b for b, m in RECOVERY.items() if m is RecoveryMode.AUTO_WHEN_CLEAR} == {
        BreakerType.DATA_QUALITY
    }


def test_reset_is_refused_while_breached_or_unknown():
    breached = Condition(BreakerType.DAILY_LOSS, True, "loss 2500 vs 2000")
    assert reset_allowed(BreakerType.DAILY_LOSS, breached)[0] is False
    unknown = Condition(BreakerType.DAILY_LOSS, None, "cannot measure")
    assert reset_allowed(BreakerType.DAILY_LOSS, unknown)[0] is False
    assert reset_allowed(BreakerType.DAILY_LOSS, None)[0] is False
    ok = Condition(BreakerType.DAILY_LOSS, False, "loss 0 vs 2000")
    assert reset_allowed(BreakerType.DAILY_LOSS, ok)[0] is True
    assert reset_allowed(BreakerType.KILL_SWITCH, None)[0] is True
    assert reset_allowed(BreakerType.EXECUTION_SAFETY, None)[0] is True

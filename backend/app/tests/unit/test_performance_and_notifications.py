"""Pure parts of the paper-account Performance page (spec §22.6) and the notification feed
(spec §23). Inputs are TEST FIXTURE numbers, not results."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.notifications import KINDS
from app.portfolio.performance import (
    MIN_RETURNS_FOR_RATIOS,
    ClosedTrade,
    Point,
    breakdown,
    daily_curve,
    daily_returns,
    drawdowns,
    monthly_returns,
    risk_ratios,
    trade_stats,
)

D = Decimal
T0 = datetime(2026, 11, 2, 21, 30, tzinfo=UTC)  # Monday 16:30 New York


def test_the_curve_keeps_the_last_complete_checkpoint_of_each_new_york_day():
    rows = [
        (T0, D("100000"), "COMPLETE"),
        (T0 + timedelta(hours=2), D("100100"), "COMPLETE"),  # same New York day: wins
        (T0 + timedelta(hours=8), D("99000"), "COMPLETE"),  # 00:30 New York Tuesday
        (T0 + timedelta(days=1), D("1"), "STALE"),  # never drawn, counted
        (T0 + timedelta(days=1, hours=1), D("1"), "INCOMPLETE"),
    ]
    points, excluded = daily_curve(reversed(rows))
    assert points == [Point(date(2026, 11, 2), D("100100")), Point(date(2026, 11, 3), D("99000"))]
    assert excluded == 2


def test_returns_drawdowns_and_months():
    points = [
        Point(date(2026, 10, 30), D("100000")),
        Point(date(2026, 11, 2), D("102000")),
        Point(date(2026, 11, 3), D("99960")),
        Point(date(2026, 11, 4), D("103000")),
    ]
    returns = daily_returns(points)
    assert [round(r, 6) for r in returns] == [0.02, -0.02, round(103000 / 99960 - 1, 6)]
    dd = dict(drawdowns(points))
    assert dd[date(2026, 11, 2)] == 0
    assert dd[date(2026, 11, 3)] == D("99960") / D("102000") - 1  # -2% from the peak
    assert dd[date(2026, 11, 4)] == 0  # a new peak
    months = monthly_returns(points)
    assert [m["month"] for m in months] == ["2026-10", "2026-11"]
    assert months[0]["return"] == "0.000000"  # one day in October: from itself
    assert months[1]["return"] == "0.030000"  # 103000 / 100000 - 1


def test_ratios_need_a_minimum_sample_and_say_so():
    few = risk_ratios([0.01] * (MIN_RETURNS_FOR_RATIOS - 1))
    assert few["sharpe"] is None
    assert few["sortino"] is None
    assert "at least" in few["reason"]
    enough = risk_ratios([0.01, -0.005] * MIN_RETURNS_FOR_RATIOS)
    assert enough["daily_returns"] == 2 * MIN_RETURNS_FOR_RATIOS
    assert enough["sharpe"] is not None
    assert Decimal(enough["sharpe"]) > 0
    assert enough["sortino"] is not None
    assert enough["reason"] is None


def trade(strategy: str, direction: str, realized: str, costs: str) -> ClosedTrade:
    return ClosedTrade(
        strategy, direction, "TAKE_PROFIT", T0, T0 + timedelta(days=2), D(realized), D(costs)
    )


def test_trade_statistics_are_net_of_costs_with_their_sample_size():
    trades = [
        trade("mean_reversion", "LONG", "300", "2"),
        trade("mean_reversion", "SHORT", "-150", "3"),
        trade("event_driven", "LONG", "1", "2"),  # a gross win that is a net loss
    ]
    stats = trade_stats(trades)
    assert stats["trades"] == 3
    assert (stats["wins"], stats["losses"]) == (1, 2)
    assert stats["net_pnl"] == "144.00"  # 298 - 153 - 1
    assert stats["costs"] == "7.00"
    assert stats["profit_factor"] == str((D(298) / D(154)).quantize(D("0.000001")))
    assert stats["expectancy"] == "48.00"
    by_strategy = {row["strategy_id"]: row for row in breakdown(trades, "strategy_id")}
    assert by_strategy["mean_reversion"]["trades"] == 2
    assert by_strategy["event_driven"]["net_pnl"] == "-1.00"
    empty = trade_stats([])
    assert (empty["trades"], empty["win_rate"], empty["profit_factor"]) == (0, None, None)


def test_the_feed_kinds_cover_the_spec_list():
    # Spec 23: opened/closed positions, important P/L, rejected orders, circuit breakers,
    # data outages / provider / worker failures / discrepancies / pauses / recoveries.
    assert set(KINDS) == {
        "POSITION_OPENED",
        "POSITION_CLOSED",
        "LARGE_PNL",
        "ORDER_REJECTED",
        "TRADE_REJECTED",
        "CIRCUIT_BREAKER",
        "SYSTEM_ALERT",
    }

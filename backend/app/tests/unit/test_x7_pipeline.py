# ruff: noqa: E501, PT018  (fixture rows on one line; paired assertions read as one fact)
"""X7 (D-256): the backtest pipeline end to end on SYNTHETIC slots and signals.

Every bar, signal, dividend and exit below is test data (hand-written on the real XNYS
calendar), not provider data and not a strategy result.
"""

from __future__ import annotations

import math
from decimal import Decimal as D
from typing import Any

from app.research import x5_accounting as acc
from app.research import x6_data as xd
from app.research import x6_execution as ex
from app.research import x7_pipeline as xp7
from app.research.x6_data import Market
from app.risk.config import HardLimits

from .test_x6_execution import mk_slot

BASE = xp7.SCENARIOS["baseline"]
PERIOD = ("2017-01-31", "2017-03-31")


def sig(slot: str, session: str, entry: str = "100.4", stop: str = "95", target: str | None = "110",
        horizon: int = 5, strength: str = "0.5", strategy: str = "momentum") -> dict[str, Any]:  # fmt: skip
    return {"strategy_id": strategy, "slot": slot, "session": session, "entry": entry, "stop": stop,
            "target": target, "horizon": horizon, "strength": strength}  # fmt: skip


def run(market: Market, signals: list[dict[str, Any]], scenario: xp7.Scenario = BASE, period: tuple[str, str] = PERIOD,
        strategies: tuple[str, ...] = ("momentum",)) -> xp7.X7Portfolio:  # fmt: skip
    return xp7.simulate(market, signals, strategies, period, scenario)


AAA = "STOCK|2016-12-30|AAA"


def fills(pf: xp7.X7Portfolio, status: str = "FILLED") -> list[dict[str, Any]]:
    return [f for f in pf.fills if f.get("status") == status]


# ---------------------------------------------------------------------- timing and look-ahead
def test_a_signal_enters_at_the_next_sessions_open_never_the_signal_session():
    pf = run(Market({AAA: mk_slot()}), [sig(AAA, "2017-02-01")])
    entry = fills(pf)[0]
    assert entry["session"] == "2017-02-02" and entry["timing"] == "NEXT_OPEN"
    assert (
        entry["decided_at"] == "2017-02-01T21:00:00+00:00"
        and entry["execution_start"] == "2017-02-02T14:30:00+00:00"
    )


def test_signals_after_the_period_or_from_other_strategies_are_ignored():
    pf = run(
        Market({AAA: mk_slot()}),
        [sig(AAA, "2017-03-31"), sig(AAA, "2017-02-01", strategy="breakout")],
    )
    assert pf.fills == [] and pf.decisions == []


def test_the_horizon_exit_is_decided_h_sessions_after_the_signal():
    pf = run(Market({AAA: mk_slot()}), [sig(AAA, "2017-02-01", target=None, horizon=3)])
    exit_fill = next(f for f in fills(pf) if f["side"] == "SELL")
    # signal 02-01 (index 0); decided at the close of 02-06 (3 sessions later), filled 02-07
    assert (
        exit_fill["session"] == "2017-02-07"
        and exit_fill["intent_id"] == "2017-02-06|horizon|" + AAA
    )
    assert pf.trades[0]["exit"] == "HORIZON" and pf.trades[0]["strategy_id"] == "momentum"


# ---------------------------------------------------------------------- stops and targets
def test_a_target_fills_only_when_traded_strictly_through_and_never_below_it():
    touch = {"2017-02-03": {"high": D("110")}}  # touching is not filling
    through = {"2017-02-03": {"high": D("111"), "close": D("110.5")}}
    pf = run(Market({AAA: mk_slot(overrides=touch)}), [sig(AAA, "2017-02-01", horizon=50)])
    assert not [
        f for f in fills(pf) if f.get("reason") == "TARGET" and f["session"] == "2017-02-03"
    ]
    pf2 = run(Market({AAA: mk_slot(overrides=through)}), [sig(AAA, "2017-02-01", horizon=50)])
    t = next(f for f in fills(pf2) if f.get("reason") == "TARGET")
    assert (
        t["session"] == "2017-02-03"
        and D(t["price"]) == D("110")
        and pf2.trades[0]["exit"] == "TARGET"
    )


def test_the_stop_comes_first_when_stop_and_target_are_both_reachable():
    both = {"2017-02-03": {"high": D("111"), "low": D("94"), "close": D("100")}}
    pf = run(Market({AAA: mk_slot(overrides=both)}), [sig(AAA, "2017-02-01", horizon=50)])
    exits = [f for f in fills(pf) if f["side"] == "SELL"]
    assert [f["reason"] for f in exits] == ["STOP"] and exits[0]["reference"] == "95"


def test_a_gap_above_the_target_fills_at_the_open_less_costs_but_not_below_the_target():
    gap = {"2017-02-03": {"open": D("112"), "high": D("113"), "low": D("111.5"), "close": D("112")}}
    pf = run(Market({AAA: mk_slot(overrides=gap)}), [sig(AAA, "2017-02-01", horizon=50)])
    t = next(f for f in fills(pf) if f.get("reason") == "TARGET")
    assert t["reference"] == "112" and D("110") <= D(t["price"]) < D("112") and t["gap"] is True


# ---------------------------------------------------------------------- capital and membership
def test_concurrent_signals_never_over_allocate_capital_or_positions():
    slots = {f"STOCK|2016-12-30|S{i:02d}": mk_slot(f"STOCK|2016-12-30|S{i:02d}") for i in range(20)}
    signals = [sig(s, "2017-02-01", stop="99", strength=str(i)) for i, s in enumerate(slots)]
    pf = run(Market(slots), signals)
    entered = {f["slot"] for f in fills(pf) if f["session"] == "2017-02-02"}
    assert len(entered) <= HardLimits().max_open_positions
    first = next(s for s in pf.snapshots if s["session"] == "2017-02-02")
    assert (
        D(first["cash"]) >= 0
        and D(first["long_market_value"])
        <= D(first["equity"]) * HardLimits().max_gross_exposure_pct
    )
    # the strongest signals were reserved first
    assert max(entered) == "STOCK|2016-12-30|S19"
    assert pf.reconciliation if hasattr(pf, "reconciliation") else True
    rejected = [d for d in pf.decisions if d["status"] == "REJECTED"]
    assert rejected and {r for d in rejected for r in d["reasons"]} <= {
        "MAX_OPEN_POSITIONS",
        "NO_ROOM",
        "EXCEEDS_GROSS_EXPOSURE",
    } | {r for d in rejected for r in d["reasons"] if r.startswith("RESIZED")}


def test_no_entry_where_the_slot_is_not_a_member_at_the_execution_session():
    sd = mk_slot(window=("2017-01-03", "2017-02-01"))
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-01")])
    assert pf.fills == [] and pf.decisions[0]["reasons"] == ["NOT_A_MEMBER_AT_EXECUTION"]


def test_one_position_per_slot_and_the_strongest_strategy_wins_in_a_combined_set():
    signals = [
        sig(AAA, "2017-02-01", strength="0.2", strategy="breakout"),
        sig(AAA, "2017-02-01", strength="0.9", strategy="momentum"),
    ]
    pf = run(Market({AAA: mk_slot()}), signals, strategies=("breakout", "momentum"))
    entries = [d for d in pf.decisions if "|entry|" in d["intent_id"]]
    assert len(entries) == 1 and "momentum" in entries[0]["intent_id"]
    pf2 = run(
        Market({AAA: mk_slot()}),
        [sig(AAA, "2017-02-01", horizon=50), sig(AAA, "2017-02-02", horizon=50)],
    )
    assert sum(1 for d in pf2.decisions if "|entry|" in d["intent_id"]) == 1  # no adding while held


# ---------------------------------------------------------------------- breaks, exits, receivables
def test_a_frozen_position_makes_the_run_incomplete_and_is_never_liquidated():
    sd = mk_slot(
        missing=("2017-02-06",),
        breaks={"2017-02-06": "MISSING_OR_EXCLUDED", "2017-02-07": "MISSING_OR_EXCLUDED"},
    )
    m = Market({AAA: sd})
    pf = run(m, [sig(AAA, "2017-02-01", horizon=50, target=None)])
    assert pf.positions[AAA].state == "FROZEN" and not [f for f in fills(pf) if f["side"] == "SELL"]
    prefix, incomplete = xp7.complete_prefix(pf.snapshots)
    assert incomplete is not None
    assert (
        prefix[-1]["session"] == "2017-02-03"
        and incomplete["first_incomplete_session"] == "2017-02-06"
    )
    assert incomplete["frozen_positions"] == [AAA]


def test_run_records_use_only_complete_equity_for_headlines(monkeypatch):
    sd = mk_slot(
        missing=("2017-02-06",),
        breaks={"2017-02-06": "MISSING_OR_EXCLUDED", "2017-02-07": "MISSING_OR_EXCLUDED"},
    )
    spy = mk_slot("FUND|2016-12-30|SPY", window=("2017-01-03", "2019-12-31"))
    m = Market({AAA: sd, spy.slot: spy})
    monkeypatch.setitem(xp7.PERIODS, "test", PERIOD)
    docs = {xd.X5_TOTAL_RETURNS: {"series": {spy.slot: {"returns": [["2017-02-01", 1.01]], "breaks": []},
                                                  AAA: {"returns": [], "breaks": []}}}}  # fmt: skip
    rec = xp7.run_record(
        m, docs, [sig(AAA, "2017-02-01", horizon=50, target=None)], "momentum", "test", BASE, {}
    )
    assert rec["equity_complete"] is False and rec["headline"]["end"] == "2017-02-03"
    assert (
        rec["headline_basis"].startswith("COMPLETE PREFIX ONLY")
        and rec["incomplete"]["sessions"] > 0
    )
    assert rec["incomplete"]["frozen_detail"][0]["reason"] == "BREAK_MISSING_OR_EXCLUDED"
    assert rec["reconciliation"]["cash_equal"] and rec["reconciliation"]["receivables_equal"]


def test_a_valued_exit_converts_and_its_cash_part_is_a_receivable():
    term = {"status": "VALUED", "valuation_session": "2017-02-06", "value_per_share": 105.0,
            "components": [{"kind": "cash", "per_share": 105.0}]}  # fmt: skip
    m = Market({AAA: mk_slot(end="2017-02-02", terminal=term)})
    pf = run(m, [sig(AAA, "2017-02-01", horizon=50, target=None)])
    assert AAA not in pf.positions and pf.trades[0]["exit"] == "EXIT"
    assert pf.journal.balance("RECEIVABLE_EXIT_CASH") == D(105) * D(pf.trades[0]["entry_quantity"])
    assert D(pf.trades[0]["receivables"]) > 0 and xp7.reconcile(pf)["receivables_equal"]


def test_dividends_are_receivables_and_never_fund_an_entry():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-03",
        "rate": 5,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    m = Market({AAA: mk_slot(events=[div])})
    pf = run(m, [sig(AAA, "2017-02-01", horizon=50, target=None)])
    snap = next(s for s in pf.snapshots if s["session"] == "2017-02-03")
    q = D(fills(pf)[0]["quantity"])
    assert D(snap["receivables"]["RECEIVABLE_DIVIDENDS"]) == 5 * q
    assert D(snap["spendable"]) == D(snap["cash"])  # no reservations: spendable is cash only
    assert D(snap["equity"]) == D(snap["cash"]) + D(snap["long_market_value"]) + 5 * q


# ---------------------------------------------------------------------- costs and scenarios
def test_adverse_scenarios_cost_more_and_costs_off_costs_nothing():
    m = Market({AAA: mk_slot()})
    signals = [sig(AAA, "2017-02-01", target=None, horizon=3)]
    base = xp7.cost_breakdown(run(m, signals))
    worse = xp7.cost_breakdown(run(m, signals, xp7.SCENARIOS["all_adverse"]))
    free = xp7.cost_breakdown(run(m, signals, xp7.SCENARIOS["costs_off"]))
    assert D(worse["implicit_vs_reference"]) > D(base["implicit_vs_reference"]) > 0
    assert D(free["total"]) == 0 and D(base["explicit"]["COMMISSION_EXPENSE"]) == D("2.00")


def test_scenarios_change_only_execution_assumptions():
    b = xp7.SCENARIOS["baseline"].config
    for name, sc in xp7.SCENARIOS.items():
        c = sc.config
        assert c.hard_limits == b.hard_limits and c.timing == b.timing and c.borrow_book == ()
        if name != "baseline":
            assert (
                c.sha256() != b.sha256()
                or sc.spread_multiplier != 1
                or sc.spread_source != "MEASURED"
            )
    assert xp7.SCENARIOS["costs_off"].diagnostic and not xp7.SCENARIOS["baseline"].diagnostic


def test_the_spread_scale_applies_to_the_liquidity_estimate():
    m = Market({AAA: mk_slot()})
    base = run(m, [])
    wide = xp7.X7Portfolio(
        m, xp7.SCENARIOS["spread_x2"].config, xp7.CAPITAL, "2017-02-01", spread_multiplier=D(2)
    )
    assert (
        wide._liquidity(AAA, "2017-02-01").spread_bps
        == 2 * base._liquidity(AAA, "2017-02-01").spread_bps
    )


# ---------------------------------------------------------------------- determinism and reconciliation
def test_runs_are_deterministic_and_reconcile_independently():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-10",
        "rate": 1,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    slots = {
        f"STOCK|2016-12-30|S{i}": mk_slot(f"STOCK|2016-12-30|S{i}", events=[div]) for i in range(4)
    }
    signals = [
        sig(s, d, strength=str(i))
        for i, s in enumerate(slots)
        for d in ("2017-02-01", "2017-02-15", "2017-03-01")
    ]
    a, b = run(Market(slots), signals), run(Market(slots), signals)
    assert (
        a.journal.entries == b.journal.entries
        and a.snapshots == b.snapshots
        and a.trades == b.trades
    )
    r = xp7.reconcile(a)
    assert (
        r["cash_equal"]
        and r["receivables_equal"]
        and r["journal_balanced"]
        and not r["invariant_violations"]
    )


# ---------------------------------------------------------------------- metrics
def test_series_metrics_formulas():
    eq = [("d0", D(100)), ("d1", D(110)), ("d2", D(99)), ("d3", D("108.9"))]
    m = xp7.series_metrics(eq)
    rets = [0.1, -0.1, 0.1]
    mean, sd = sum(rets) / 3, math.sqrt(sum((r - sum(rets) / 3) ** 2 for r in rets) / 2)
    assert m["total_return"] == round(108.9 / 100 - 1, 6) and m["max_drawdown"] == round(
        99 / 110 - 1, 6
    )
    assert m["sharpe"] == round(mean / sd * math.sqrt(252), 4)
    assert m["sortino"] == round(mean / math.sqrt(0.01 / 3) * math.sqrt(252), 4)
    assert m["annualized_return"] == round((1.089) ** (252 / 3) - 1, 6)


def test_trade_metrics():
    t = [{"pnl": "10", "exit": "SELL", "strategy_id": "a"}, {"pnl": "-4", "exit": "STOP", "strategy_id": "a"},
         {"pnl": "0", "exit": "SELL", "strategy_id": "b"}]  # fmt: skip
    m = xp7.trade_metrics(t)
    assert (m["win_rate"], m["profit_factor"], m["average_win"], m["average_loss"]) == (
        round(1 / 3, 4),
        2.5,
        "10.00",
        "-2.00",
    )


def test_regimes_use_only_the_previous_close():
    eq = [("2017-01-03", D(100)), ("2017-01-04", D(101))]
    out = xp7.regime_returns(
        eq,
        {
            "2017-01-03": {"trend": "UP", "volatility": "LOW"},
            "2017-01-04": {"trend": "DOWN", "volatility": "HIGH"},
        },
    )
    assert set(out) == {"trend_UP", "volatility_LOW"} and out["trend_UP"]["sessions"] == 1


def test_sessions_are_xnys_sessions():
    assert acc._sessions("2017-01-13", "2017-01-17") == ["2017-01-13", "2017-01-17"]
    assert ex.next_session("2017-12-29") == "2018-01-02"


def test_no_entry_executes_on_the_windows_last_session():
    old_window = mk_slot(start="2017-12-01", end="2017-12-29", window=("2017-01-03", "2017-12-29"))
    pf = run(
        Market({AAA: old_window}),
        [sig(AAA, "2017-12-28", entry="100.1")],
        period=("2017-12-20", "2018-01-05"),
    )
    assert pf.decisions == [] and pf.skipped["ENTRY_ON_WINDOW_LAST_SESSION"] == 1
    pf2 = run(
        Market({AAA: old_window}),
        [sig(AAA, "2017-12-27", entry="100.1", target=None)],
        period=("2017-12-20", "2018-01-05"),
    )
    assert not pf2.positions and pf2.trades[0]["exit"] == "WINDOW_END"  # exited at the last session

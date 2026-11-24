# ruff: noqa: E501, PT018  (fixture rows on one line; paired assertions read as one fact)
"""X6 (D-255): execution pricing, liquidity, shorts, accounting, corporate actions, formation
steps and breaks, on SYNTHETIC slots (hand-written bars on the real XNYS calendar).

Every bar, dividend, distribution and exit below is test data, not provider data.
"""

from __future__ import annotations

import itertools
import math
from decimal import Decimal as D
from typing import Any

import pytest

from app.research import x5_accounting as acc
from app.research import x6_data as xd
from app.research import x6_execution as ex
from app.research import x6_portfolio as xp
from app.research.x6_data import Bar, Market, SlotData
from app.research.x6_execution import Side, X6Config
from app.risk.config import HardLimits

NO_STOP = HardLimits(require_stop=False)
CFG = X6Config(hard_limits=NO_STOP)


def mk_slot(
    name: str = "STOCK|2016-12-30|AAA",
    start: str = "2017-01-03",
    end: str = "2017-03-31",
    *,
    window: tuple[str, str] = ("2017-01-03", "2017-12-29"),
    formation: str = "2016-12-30",
    usable: tuple[str, str] | None = None,
    price: str = "100",
    volume: int = 1_000_000,
    overrides: dict[str, dict[str, Any]] | None = None,
    missing: tuple[str, ...] = (),
    not_executable: tuple[str, ...] = (),
    breaks: dict[str, str] | None = None,
    events: list[dict[str, Any]] | None = None,
    terminal: dict[str, Any] | None = None,
    formation_close: D | None = D("100"),
    mdv: D | None = D("100000000"),
    has_series: bool = True,
) -> SlotData:
    bars: dict[str, Bar] = {}
    for i, s in enumerate(acc._sessions(start, end)):
        if s in missing:
            continue
        c = D(price) + D(i % 5) / 10
        row = {
            "open": c - D("0.05"),
            "high": c + D("0.5"),
            "low": c - D("0.5"),
            "close": c,
            "volume": D(volume),
            "vwap": c,
        }
        row.update((overrides or {}).get(s, {}))
        bars[s] = Bar(
            s, row["open"], row["high"], row["low"], row["close"], row["volume"], row["vwap"]
        )
    evs = events or []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for e in evs:
        by_date.setdefault(e["date"], []).append(e)
    pool, _, ticker = name.split("|")
    sd = SlotData(slot=name, ticker=ticker, ledger_key=f"KEY:{ticker}", pool=pool, formation=formation,
                  window=window, status="PASS", usable_range=usable or (start, end), bars=bars,
                  not_executable=frozenset(not_executable), breaks=breaks or {}, events=evs, terminal=terminal,
                  has_series=has_series, formation_close=formation_close, formation_median_dollar_volume=mdv,
                  last_usable=(usable or (start, end))[1], by_date=by_date)  # fmt: skip
    return sd


def market(*slots: SlotData) -> Market:
    return Market({s.slot: s for s in slots})


def advance(pf: xp.Portfolio, n: int = 1, intents: list[xp.OrderIntent] | None = None) -> None:
    for i in range(n):
        if i == 0 and intents:
            pf.decide(intents)
        pf.begin_session(ex.next_session(pf.session))
        pf.close_session()


def buy(i: str, qty: int, slot: str = "STOCK|2016-12-30|AAA", **kw: Any) -> xp.OrderIntent:
    return xp.OrderIntent(i, slot, Side.BUY, D(qty), **kw)


LIQ = ex.Liquidity(
    "2017-01-03", "HISTORY", 20, D(1_000_000), D(10), "ABDI_RANALDO", D("0.02"), "HISTORY"
)
BAR = Bar("2017-01-04", D("100.01"), D("101"), D("99"), D("100.5"), D(1_000_000), D("100.2"))


# ---------------------------------------------------------------------- execution pricing
def test_buy_and_sell_prices_add_half_spread_slippage_and_impact_adversely_to_the_tick():
    f = ex.quote_fill(Side.BUY, D(10_000), BAR, LIQ, CFG)
    # half spread 5 + slippage 5 (risk Preferences) + impact 1 * 0.02 * sqrt(0.01) = 20 bps
    assert f.bps == {
        "half_spread": D("5.00"),
        "slippage": D("5"),
        "impact": D("20.00"),
        "total": D("30.00"),
    }
    assert f.reference == D("100.01") and f.price == D("100.32")  # 100.31003 up to the tick
    s = ex.quote_fill(Side.SELL, D(10_000), BAR, LIQ, CFG)
    assert s.price == D("99.70")  # 99.70997 down to the tick
    assert f.commission == D("50.00") and f.fees == ceil(D("100.32") * 10_000 * D("0.0001"))
    assert f.regulatory_fee == 0 and s.regulatory_fee == 0  # no dated schedule configured


def ceil(v: D) -> D:
    return ex.ceil_cents(v)


def test_a_fill_is_never_outside_the_sessions_traded_range():
    wide = ex.Liquidity(
        "x", "HISTORY", 20, D(1_000_000), D(2000), "ABDI_RANALDO", D("0.02"), "HISTORY"
    )
    f = ex.quote_fill(Side.BUY, D(100), BAR, wide, CFG)
    s = ex.quote_fill(Side.SELL, D(100), BAR, wide, CFG)
    assert (f.price, f.clamped) == (
        BAR.high,
        True,
    ) and "PRICE_HELD_INSIDE_SESSION_RANGE" in f.reasons
    assert (s.price, s.clamped) == (BAR.low, True)


def test_reference_price_follows_the_configured_timing():
    assert ex.reference_price(BAR, ex.Timing.NEXT_OPEN) == BAR.open
    assert ex.reference_price(BAR, ex.Timing.NEXT_VWAP) == BAR.vwap
    assert ex.reference_price(BAR, ex.Timing.NEXT_CLOSE) == BAR.close
    with pytest.raises(ValueError, match="no VWAP"):
        ex.reference_price(
            Bar("2017-01-04", D(1), D(1), D(1), D(1), D(1), None), ex.Timing.NEXT_VWAP
        )


def test_limit_orders_do_not_fill_beyond_their_limit():
    f = ex.quote_fill(Side.BUY, D(100), BAR, LIQ, CFG, limit=D("100.00"))
    assert f.quantity == 0 and "LIMIT_NOT_REACHED" in f.reasons


def test_costs_scale_with_the_square_root_of_participation_and_commission_has_a_minimum():
    assert ex.impact_bps(D(40_000), LIQ, CFG) == 2 * ex.impact_bps(D(10_000), LIQ, CFG)
    assert ex.commission(D(10), CFG) == D("1.00") and ex.commission(D(0), CFG) == 0
    sched = X6Config(
        regulatory_fee_schedule=(
            ex.FeeRate(effective="2017-01-01", rate=D("0.0000218"), source="fixture schedule"),
        )
    )
    assert ex.regulatory_fee(D(100_000), Side.SELL, "2017-01-04", sched) == D("2.18")
    assert ex.regulatory_fee(D(100_000), Side.BUY, "2017-01-04", sched) == 0
    assert (
        ex.regulatory_fee(D(100_000), Side.SELL, "2016-12-30", sched) == 0
    )  # before its effective date


def test_decisions_are_at_the_close_and_executions_in_the_next_xnys_session():
    t = ex.execution_times("2017-01-03", "2017-01-04", ex.Timing.NEXT_OPEN)
    assert t == {"decided_at": "2017-01-03T21:00:00+00:00", "execution_start": "2017-01-04T14:30:00+00:00",
                 "execution_end": "2017-01-04T14:30:00+00:00"}  # fmt: skip
    assert ex.next_session("2017-01-13") == "2017-01-17"  # Martin Luther King, Jr. Day
    early = ex.execution_times("2017-11-22", "2017-11-24", ex.Timing.NEXT_CLOSE)
    assert early["execution_end"] == "2017-11-24T18:00:00+00:00"  # early close
    with pytest.raises(ValueError, match="follow"):
        ex.execution_times("2017-01-04", "2017-01-04", ex.Timing.NEXT_OPEN)


# ---------------------------------------------------------------------- liquidity estimates
def test_liquidity_uses_only_bars_up_to_the_decision():
    sd = mk_slot()
    hist = list(sd.bars.values())[:30]
    with pytest.raises(ValueError, match="look-ahead"):
        ex.estimate_liquidity(hist, hist[10].session, CFG)
    liq = ex.estimate_liquidity(hist, hist[-1].session, CFG)
    assert liq.basis == "HISTORY" and liq.adv_shares == 1_000_000 and liq.sessions == 20
    assert liq.spread_basis == "ABDI_RANALDO" and liq.volatility_basis == "HISTORY"


def test_abdi_ranaldo_spread_from_close_high_low():
    # Close at the high, then a session centred one percent lower: s^2 = 4 (c - eta_t)(c - eta_t+1).
    sessions = acc._sessions("2017-01-03", "2017-02-28")
    bars = [Bar(s, D(100), D(101) if i % 2 == 0 else D(100), D(99), D(101) if i % 2 == 0 else D(99), D(10), None)
            for i, s in enumerate(sessions)]  # fmt: skip
    liq = ex.estimate_liquidity(bars[:25], bars[24].session, CFG)
    pairs = list(itertools.pairwise(bars[4:25]))
    s2 = []
    for a, b in pairs:
        c = math.log(float(a.close))
        s2.append(max(4 * (c - (math.log(float(a.high)) + math.log(float(a.low))) / 2)
                      * (c - (math.log(float(b.high)) + math.log(float(b.low))) / 2), 0.0))  # fmt: skip
    assert liq.spread_bps == D(repr(math.sqrt(sum(s2) / len(s2)) * 10000)).quantize(D("0.01"))
    assert liq.spread_bps > 50  # wide synthetic bars


def test_short_history_falls_back_to_the_formation_record_and_labelled_assumptions():
    sd = mk_slot()
    liq = ex.estimate_liquidity(
        list(sd.bars.values())[:3], "2017-01-05", CFG, formation=(D(50), D(5_000_000))
    )
    assert (liq.basis, liq.adv_shares) == ("FORMATION_RECORD", D(100_000))
    assert (liq.spread_bps, liq.spread_basis) == (
        CFG.preferences.fallback_spread_bps,
        "ASSUMED_FALLBACK",
    )
    assert (liq.daily_volatility, liq.volatility_basis) == (CFG.assumed_daily_volatility, "ASSUMED")
    none = ex.estimate_liquidity([], "2017-01-03", CFG)
    assert none.basis == "NONE" and none.adv_shares is None


def test_volumes_before_a_split_are_restated_in_post_split_shares():
    sd = mk_slot(volume=1000)
    hist = list(sd.bars.values())[:20]
    split_day = hist[10].session
    liq = ex.estimate_liquidity(hist, hist[-1].session, CFG, share_ratios={split_day: D(2)})
    assert liq.adv_shares == (D(10 * 2000 + 10 * 1000) / 20).quantize(D(1))


# ---------------------------------------------------------------------- liquidity limits
def test_a_fill_takes_at_most_the_configured_share_of_the_sessions_volume():
    small = Bar("2017-01-04", D(100), D(101), D(99), D(100), D(1000), D(100))
    f = ex.quote_fill(Side.BUY, D(500), small, LIQ, CFG)
    assert f.quantity == 50 and "CAPPED_BY_SESSION_VOLUME" in f.reasons  # 5% of 1,000


def test_entries_are_resized_to_adv_participation_or_rejected_by_policy():
    sd = mk_slot(volume=100_000)
    pf = xp.Portfolio(market(sd), CFG, D(10_000_000), "2017-02-01")
    d = pf.decide([buy("a", 5_000)])[0]
    assert (
        d["status"] == "RESIZED"
        and "RESIZED_TO_ADV_PARTICIPATION" in d["reasons"]
        and d["quantity"] == "1000"
    )
    strict = X6Config(hard_limits=NO_STOP, oversize_policy=ex.OversizePolicy.REJECT)
    pf2 = xp.Portfolio(market(sd), strict, D(10_000_000), "2017-02-01")
    d2 = pf2.decide([buy("a", 5_000)])[0]
    assert d2["status"] == "REJECTED" and "EXCEEDS_ADV_PARTICIPATION" in d2["reasons"]


def test_an_entry_with_a_spread_above_the_limit_is_rejected():
    wide = {
        s: {"high": D(110), "low": D(90), "close": D(110) if i % 2 else D(90)}
        for i, s in enumerate(acc._sessions("2017-01-03", "2017-03-31"))
    }
    sd = mk_slot(overrides=wide)
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    assert pf.decide([buy("a", 10)])[0]["reasons"] == ["SPREAD_ABOVE_LIMIT"]


# ---------------------------------------------------------------------- shorts
def short(i: str, qty: int, **kw: Any) -> xp.OrderIntent:
    return xp.OrderIntent(i, "STOCK|2016-12-30|AAA", Side.SELL_SHORT, D(qty), **kw)


BORROW = ex.BorrowAssumption(
    ticker="AAA",
    start="2017-01-01",
    end="2017-03-31",
    annual_rate=D("0.036"),
    source="fixture borrow assumption",
)
SCFG = X6Config(hard_limits=NO_STOP, borrow_book=(BORROW,))


def test_shorts_are_never_assumed_borrowable():
    pf = xp.Portfolio(market(mk_slot()), CFG, D(1_000_000), "2017-02-01")
    assert pf.decide([short("s", 100)])[0]["reasons"] == ["SHORT_BORROW_UNAVAILABLE"]
    late = X6Config(
        hard_limits=NO_STOP, borrow_book=(BORROW.model_copy(update={"start": "2017-02-02"}),)
    )
    pf2 = xp.Portfolio(market(mk_slot()), late, D(1_000_000), "2017-02-01")
    assert pf2.decide([short("s", 100)])[0]["reasons"] == [
        "SHORT_BORROW_UNAVAILABLE"
    ]  # not at the decision
    off = X6Config(
        hard_limits=HardLimits(require_stop=False, shorts_enabled=False), borrow_book=(BORROW,)
    )
    pf3 = xp.Portfolio(market(mk_slot()), off, D(1_000_000), "2017-02-01")
    assert pf3.decide([short("s", 100)])[0]["reasons"] == ["SHORTS_DISABLED"]


def test_a_short_restricts_its_proceeds_reserves_margin_and_pays_borrow_and_dividends():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-06",
        "rate": 0.5,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    sd = mk_slot(events=[div])
    pf = xp.Portfolio(market(sd), SCFG, D(1_000_000), "2017-02-01")
    d = pf.decide([short("s", 1000)])[0]
    assert d["status"] == "APPROVED"
    advance(pf)  # 2017-02-02 open
    fill = pf.fills[-1]
    proceeds = D(fill["price"]) * 1000
    snap = pf.snapshots[-1]
    assert D(snap["short_proceeds_restricted"]) == proceeds

    def borrow(snapshot: dict[str, Any], nights: int) -> D:
        return ex.ceil_cents(D(snapshot["short_market_value"]) * D("0.036") * nights / 360)

    # Proceeds are not cash; the first night's borrow is charged at the close.
    assert D(snap["cash"]) == D(1_000_000) - D(fill["commission"]) - D(fill["fees"]) - borrow(
        snap, 1
    )
    assert D(snap["margin_required"]) == D(snap["short_market_value"]) * D("0.5")
    assert D(snap["spendable"]) == D(snap["cash"]) - D(snap["margin_required"])
    cash_before = pf.cash
    advance(pf)  # 2017-02-03 (Friday): three nights to Monday
    assert cash_before - pf.cash == borrow(pf.snapshots[-1], 3)
    cash_before = pf.cash
    advance(pf)  # 2017-02-06 (Monday): the dividend owed, and one night
    assert cash_before - pf.cash == D(500) + borrow(pf.snapshots[-1], 1)
    assert pf.journal.balance("SHORT_DIVIDEND_EXPENSE") == D(500) and pf.receivables_total() == 0


def test_rule_201_rejects_restricted_short_sales():
    prev = Bar("2017-01-04", D(100), D(100), D(89), D(90), D(10), None)
    nxt = Bar("2017-01-05", D(91), D(92), D(90), D(91), D(10), None)
    assert (
        ex.rule_201_restricted(D(100), prev, nxt, ex.Timing.NEXT_OPEN)
        == "RULE_201_TRIGGERED_PREVIOUS_SESSION"
    )
    calm = Bar("2017-01-04", D(100), D(101), D(99), D(100), D(10), None)
    gap = Bar("2017-01-05", D(89), D(92), D(88), D(91), D(10), None)
    assert (
        ex.rule_201_restricted(D(100), calm, gap, ex.Timing.NEXT_OPEN)
        == "RULE_201_TRIGGERED_AT_OPEN"
    )
    later = Bar("2017-01-05", D(99), D(100), D(89), D(95), D(10), None)
    assert ex.rule_201_restricted(D(100), calm, later, ex.Timing.NEXT_OPEN) is None
    assert (
        ex.rule_201_restricted(D(100), calm, later, ex.Timing.NEXT_CLOSE)
        == "RULE_201_TRIGGERED_IN_SESSION"
    )


def test_a_short_whose_borrow_ends_is_covered_automatically():
    end = X6Config(
        hard_limits=NO_STOP, borrow_book=(BORROW.model_copy(update={"end": "2017-02-03"}),)
    )
    pf = xp.Portfolio(market(mk_slot()), end, D(1_000_000), "2017-02-01")
    pf.decide([short("s", 100)])
    advance(
        pf, 2
    )  # filled 2017-02-02; at the 2017-02-03 close the borrow ends before the next session
    pf.decide()
    assert (
        pf.decisions[-1]["intent_id"].startswith("auto:borrow-end")
        and pf.decisions[-1]["side"] == "BUY_TO_COVER"
    )
    advance(pf)
    assert not pf.positions and pf.journal.balance("SHORT_PROCEEDS_RESTRICTED") == 0


# ---------------------------------------------------------------------- accounting invariants
def test_a_round_trip_balances_and_books_realized_pnl_and_costs():
    pf = xp.Portfolio(market(mk_slot()), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 500)])
    b = pf.fills[-1]
    advance(pf, intents=[xp.OrderIntent("s", "STOCK|2016-12-30|AAA", Side.SELL, D(500))])
    s = pf.fills[-1]
    realized = (D(s["price"]) - D(b["price"])) * 500
    assert -pf.journal.balance("REALIZED_PNL") == realized
    costs = sum(D(f[k]) for f in (b, s) for k in ("commission", "fees", "regulatory_fee"))
    assert pf.cash == D(1_000_000) + realized - costs
    assert D(pf.snapshots[-1]["equity"]) == pf.cash and not pf.positions
    assert sum(pf.journal.balances.values()) == 0 and pf.violations == []


def test_reservations_make_limits_atomic_across_intents_and_ids_are_idempotent():
    pf = xp.Portfolio(market(mk_slot()), CFG, D(100_000), "2017-02-01")
    out = pf.decide([buy("a", 900), buy("b", 900), buy("a", 10)])
    by = {(d["intent_id"], d["status"]) for d in out}
    assert ("a", "RESIZED") in by or ("a", "APPROVED") in by
    second = next(d for d in out if d["intent_id"] == "b")
    first = next(d for d in out if d["intent_id"] == "a" and d["status"] != "REJECTED")
    # the 20% single-asset limit is shared: the second sees the first's reservation
    assert D(first["quantity"]) + D(second["quantity"] or 0) <= D(20_000) / D("100.4")
    assert any(d["reasons"] == ["DUPLICATE_INTENT_ID"] for d in out)
    assert pf.spendable() == pf.cash - sum(r.cash for r in pf.reservations)


def test_the_number_of_open_positions_is_limited():
    slots = [mk_slot(f"STOCK|2016-12-30|A{i}") for i in range(9)]
    pf = xp.Portfolio(market(*slots), CFG, D(10_000_000), "2017-02-01")
    out = pf.decide([buy(f"i{i}", 10, slot=s.slot) for i, s in enumerate(slots)])
    assert [d["reasons"] for d in out if d["status"] == "REJECTED"] == [["MAX_OPEN_POSITIONS"]]


def test_a_buy_that_gaps_beyond_its_reservation_is_resized_to_cash_never_overdrawn():
    gap = {
        "2017-02-02": {
            "open": D(150),
            "high": D(151),
            "low": D(149),
            "close": D(150),
            "vwap": D(150),
        }
    }
    pf = xp.Portfolio(
        market(mk_slot(overrides=gap)),
        X6Config(
            hard_limits=HardLimits(
                require_stop=False, max_single_asset_pct=D(1), max_gross_exposure_pct=D(1)
            )
        ),
        D(10_000),
        "2017-02-01",
    )
    pf.decide([buy("b", 95)])
    advance(pf)
    f = pf.fills[-1]
    assert "RESIZED_TO_CASH" in f["reasons"] and pf.cash >= 0
    assert D(f["quantity"]) * D(f["price"]) + D(f["commission"]) + D(f["fees"]) <= D(10_000)


def test_loss_breakers_block_entries_but_not_exits():
    crash = {
        s: {"open": D(80), "high": D(81), "low": D(79), "close": D(80), "vwap": D(80)}
        for s in acc._sessions("2017-02-03", "2017-03-31")
    }
    pf = xp.Portfolio(market(mk_slot(overrides=crash)), CFG, D(100_000), "2017-02-01")
    advance(pf, intents=[buy("b", 190)])
    advance(pf)  # -20% on the position: about -3.8% of equity
    assert "DAILY_LOSS" in pf.snapshots[-1]["entries_blocked"]
    out = pf.decide(
        [buy("more", 10), xp.OrderIntent("out", "STOCK|2016-12-30|AAA", Side.SELL, D(190))]
    )
    assert {d["intent_id"]: d["status"] for d in out} == {"more": "REJECTED", "out": "APPROVED"}


def test_stops_fill_at_the_stop_or_at_a_gapped_open():
    path = {
        "2017-02-03": {"open": D(99), "high": D(100), "low": D(94), "close": D(96), "vwap": D(97)}
    }
    pf = xp.Portfolio(market(mk_slot(overrides=path)), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100, stop_price=D(95))])
    advance(pf)
    stop = pf.fills[-1]
    assert stop["reason"] == "STOP" and stop["reference"] == "95" and stop["gap"] is False
    gapped = {
        "2017-02-03": {"open": D(90), "high": D(92), "low": D(89), "close": D(91), "vwap": D(91)}
    }
    pf2 = xp.Portfolio(market(mk_slot(overrides=gapped)), CFG, D(1_000_000), "2017-02-01")
    advance(pf2, intents=[buy("b", 100, stop_price=D(95))])
    advance(pf2)
    assert (
        pf2.fills[-1]["reference"] == "90"
        and pf2.fills[-1]["gap"] is True
        and D(pf2.fills[-1]["price"]) < 90
    )


def test_a_stop_is_required_by_the_default_risk_limits():
    pf = xp.Portfolio(market(mk_slot()), X6Config(), D(100_000), "2017-02-01")
    assert pf.decide([buy("b", 10)])[0]["reasons"] == ["STOP_REQUIRED"]
    d = pf.decide([buy("c", 1000, stop_price=D(90))])[0]
    assert "RESIZED_TO_RISK_PER_TRADE" in d["reasons"] and D(d["quantity"]) * D("10.2") <= D(500)


# ---------------------------------------------------------------------- receivables
def test_a_dividend_is_a_receivable_for_holders_at_the_previous_close_only():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-03",
        "rate": 1.25,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    sd = mk_slot(events=[div])
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("held", 100)])  # held at the 2017-02-02 close
    spendable = pf.spendable()
    pf.decide([buy("late", 50)])  # fills at the ex-date open: not entitled
    pf.begin_session("2017-02-03")
    pf.close_session()
    assert pf.journal.balance("RECEIVABLE_DIVIDENDS") == D("125.00")
    assert pf.positions[sd.slot].quantity == 150
    assert pf.receivables == [{"kind": "DIVIDEND", "slot": sd.slot, "ex_date": "2017-02-03", "amount": "125.00",
                               "pay_date": None, "provider_id": "fx"}]  # fmt: skip
    late = pf.fills[-1]
    assert pf.spendable() == spendable - D(late["quantity"]) * D(late["price"]) - D(
        late["commission"]
    ) - D(late["fees"])
    advance(pf, 20)
    assert pf.journal.balance("RECEIVABLE_DIVIDENDS") == D("125.00")  # never becomes cash


def test_a_withholding_rate_applies_only_when_configured():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-03",
        "rate": 1,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    cfg = X6Config(hard_limits=NO_STOP, withholding_rates={"AAA": D("0.15")})
    pf = xp.Portfolio(market(mk_slot(events=[div])), cfg, D(1_000_000), "2017-02-01")
    advance(pf, 2, intents=[buy("b", 100)])
    assert pf.journal.balance("RECEIVABLE_DIVIDENDS") == D(85) and pf.journal.balance(
        "WITHHOLDING_EXPENSE"
    ) == D(15)


def exiting(status: str = "VALUED", **kw: Any) -> SlotData:
    term = {"status": status, "valuation_session": "2017-02-06", "value_per_share": 55.0,
            "components": [{"kind": "cash", "per_share": 30.0}, {"kind": "stock", "ratio": "1/2", "close": 50.0, "session": "2017-02-06", "ticker": "ACQ"}], **kw}  # fmt: skip
    return mk_slot(end="2017-02-02", terminal=term)


def test_a_valued_exit_books_cash_as_receivable_and_received_shares_as_spendable_proceeds():
    sd = exiting()
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100)])
    cost = pf.positions[sd.slot].cost
    advance(pf)  # 2017-02-03: past the usable range, awaiting the exit
    assert pf.positions[sd.slot].state == "AWAITING_EXIT"
    cash = pf.cash
    advance(pf)  # 2017-02-06: valuation session
    assert sd.slot not in pf.positions
    assert pf.journal.balance("RECEIVABLE_EXIT_CASH") == D(3000)
    stock = D(50) * D(50) * (1 - D("17.5") / 10000)  # half the fallback spread + slippage
    assert pf.cash - cash == stock - D("1.00")
    assert -pf.journal.balance("REALIZED_PNL") == D(3000) + stock - cost


@pytest.mark.parametrize(
    "status",
    ["VALUED_APPROXIMATE", "LOWER_BOUND_CVR_UNVALUED", "UNRESOLVED", "OUT_OF_USABLE_RANGE"],
)
def test_an_exit_without_a_valued_terminal_step_is_frozen_never_valued(status):
    sd = exiting(status, bounds={"all_cash": 54.0})
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100)])
    mark = pf.positions[sd.slot].mark
    advance(pf, 3)
    pos = pf.positions[sd.slot]
    assert (pos.state, pos.reason, pos.mark) == ("FROZEN", status, mark)
    assert pf.snapshots[-1]["equity_complete"] is False and pf.receivables_total() == 0
    assert next(e for e in pf.events if e["event"] == "FROZEN")["bounds"] == {"all_cash": 54.0}
    assert pf.decide([xp.OrderIntent("x", sd.slot, Side.SELL, D(100))])[0]["reasons"] == [
        "NO_TRADABLE_POSITION"
    ]


# ---------------------------------------------------------------------- corporate actions
def test_a_split_scales_quantity_mark_and_stop_and_a_fraction_is_cash_in_lieu():
    split = {"type": "STOCK_SPLIT", "date": "2017-02-03", "ratio": 1.5}
    after = {
        s: {"open": D(66), "high": D(67), "low": D(65), "close": D(66), "vwap": D(66)}
        for s in acc._sessions("2017-02-03", "2017-03-31")
    }
    sd = mk_slot(events=[split], overrides=after)
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 101, stop_price=D(90))])
    cost = pf.positions[sd.slot].cost
    advance(pf)
    pos = pf.positions[sd.slot]
    assert pos.quantity == 151 and pos.cost == cost and pos.stop == D(60)
    assert pf.journal.balance("RECEIVABLE_EXIT_CASH") == D("0.5") * D(
        66
    )  # 151.5 shares: half a share in lieu


def spin(treatment: str = "APPLIED") -> dict[str, Any]:
    return {"type": "SPIN_OFF", "date": "2017-02-03", "share_factor": "1", "distribution_value_per_share": 5.0,
            "components": [{"ticker": "KID", "ratio": "1/2", "close": 10.0, "session": "2017-02-03"}],
            "tr_treatment": treatment, "basis": "DOCUMENTED"}  # fmt: skip


def test_an_applied_spin_off_sells_the_distribution_at_its_close_and_only_then_is_it_spendable():
    sd = mk_slot(events=[spin()])
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100)])
    spendable = pf.spendable()
    pf.begin_session("2017-02-03")
    assert pf.spendable() == spendable  # not at the ex-date's open
    pf.close_session()
    proceeds = D(50) * D(10) * (1 - D("17.5") / 10000) - D("1.00")
    assert pf.spendable() == spendable + proceeds and pf.positions[sd.slot].quantity == 100


@pytest.mark.parametrize(
    "treatment",
    [
        "RESTRICTED_APPROXIMATE_RATIO",
        "RESTRICTED_PROVIDER_UNRECONCILED",
        "RESTRICTED_RATIO_UNDOCUMENTED",
    ],
)
def test_a_restricted_spin_off_freezes_a_position_held_through_it(treatment):
    sd = mk_slot(events=[spin(treatment)], breaks={"2017-02-03": treatment})
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100)])
    mark = pf.positions[sd.slot].mark
    advance(pf, 5)
    pos = pf.positions[sd.slot]
    assert (pos.state, pos.reason, pos.mark, pos.mark_session) == (
        "FROZEN",
        "BREAK_" + treatment,
        mark,
        "2017-02-02",
    )
    assert pf.snapshots[-1]["frozen"] == [sd.slot] and pf.cash + pf.receivables_total() == D(
        1_000_000
    ) - pos.cost - sum(D(f[k]) for f in pf.fills for k in ("commission", "fees"))


def test_a_position_opened_after_a_restricted_ex_date_is_valued_normally():
    sd = mk_slot(
        events=[spin("RESTRICTED_PROVIDER_UNRECONCILED")],
        breaks={"2017-02-03": "RESTRICTED_PROVIDER_UNRECONCILED"},
    )
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-03")
    advance(pf, 3, intents=[buy("b", 100)])
    assert (
        pf.positions[sd.slot].state == "OPEN" and pf.positions[sd.slot].mark_session == pf.session
    )


# ---------------------------------------------------------------------- breaks
def test_a_missing_session_freezes_a_held_position_and_is_never_bridged():
    sd = mk_slot(
        missing=("2017-02-06",),
        breaks={"2017-02-06": "MISSING_OR_EXCLUDED", "2017-02-07": "MISSING_OR_EXCLUDED"},
    )
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    advance(pf, intents=[buy("b", 100)])
    advance(pf, 5)
    pos = pf.positions[sd.slot]
    assert (
        pos.state == "FROZEN"
        and pos.mark_session == "2017-02-03"
        and pos.reason == "BREAK_MISSING_OR_EXCLUDED"
    )


def test_an_order_for_a_non_executable_session_does_not_fill_and_its_reservation_is_released():
    sd = mk_slot(not_executable=("2017-02-02",))
    pf = xp.Portfolio(market(sd), CFG, D(1_000_000), "2017-02-01")
    pf.decide([buy("b", 100)])
    assert pf.spendable() < pf.cash
    advance(pf)
    assert pf.fills[-1] == {"intent_id": "b", "slot": sd.slot, "session": "2017-02-02", "status": "NO_FILL",
                            "reasons": ["NOT_EXECUTABLE_SESSION"]}  # fmt: skip
    assert pf.spendable() == pf.cash == D(1_000_000) and not pf.reservations


def test_entries_need_membership_and_a_usable_slot():
    pf = xp.Portfolio(
        market(mk_slot(window=("2017-01-03", "2017-02-01"))), CFG, D(1_000_000), "2017-02-01"
    )
    assert pf.decide([buy("b", 10)])[0]["reasons"] == ["NOT_A_MEMBER_AT_EXECUTION"]
    pf2 = xp.Portfolio(market(mk_slot(has_series=False)), CFG, D(1_000_000), "2017-02-01")
    assert pf2.decide([buy("b", 10)])[0]["reasons"] == ["SLOT_NOT_USABLE"]


# ---------------------------------------------------------------------- formation steps
def pair(div_rate: float | None = 0.75, prev_close: str = "100") -> tuple[SlotData, SlotData]:
    old = mk_slot(
        "STOCK|2016-12-30|AAA", "2017-12-01", "2017-12-29", window=("2017-01-03", "2017-12-29")
    )
    last = {"2017-12-29": {"close": D(prev_close)}}
    old = mk_slot(
        "STOCK|2016-12-30|AAA",
        "2017-12-01",
        "2017-12-29",
        window=("2017-01-03", "2017-12-29"),
        overrides=last,
    )
    events = (
        [
            {
                "type": "CASH_DIVIDEND",
                "date": "2018-01-02",
                "rate": div_rate,
                "status": "COUNTED",
                "provider_id": "fx",
            }
        ]
        if div_rate
        else []
    )
    new = mk_slot("STOCK|2017-12-29|AAA", "2018-01-02", "2018-02-28", window=("2018-01-02", "2018-12-31"),
                  formation="2017-12-29", events=events, formation_close=D("100"),
                  overrides={"2018-01-02": {"close": D("99.5")}})  # fmt: skip
    return old, new


def test_the_formation_step_includes_a_first_session_dividend():
    old, new = pair()
    step = xd.formation_step(new, market(old, new))
    assert step["status"] == "COMPUTED" and step["gross"] == str(
        round((D("99.5") + D("0.75")) / 100, 10)
    )
    assert step["price_gross"] == "0.995" or D(step["price_gross"]) == D("0.995")
    assert step["cross_check"] == {"slot": old.slot, "close": "100", "equal": True}


def test_the_formation_step_refuses_conflicting_or_missing_evidence():
    old, new = pair(prev_close="101")
    assert xd.formation_step(new, market(old, new))["status"] == "FORMATION_CLOSE_CONFLICT"
    _, new2 = pair()
    new2.formation_close = None
    assert xd.formation_step(new2)["status"] == "NO_FORMATION_CLOSE"
    _, new3 = pair()
    new3.usable_range = ("2018-01-03", "2018-02-28")
    assert xd.formation_step(new3)["status"] == "FIRST_SESSION_NOT_USABLE"


def test_a_rolled_over_position_earns_the_first_session_dividend_once_and_a_new_buy_does_not():
    old, new = pair()
    m = market(old, new)
    cfg = X6Config(hard_limits=NO_STOP, window_end_policy=ex.WindowEndPolicy.ROLL_OVER_IF_MEMBER)
    pf = xp.Portfolio(m, cfg, D(1_000_000), "2017-12-26")
    advance(pf, intents=[buy("b", 100, slot=old.slot)])  # filled 2017-12-27
    advance(pf, 2)  # through the 2017-12-29 close: rolled over
    assert list(pf.positions) == [new.slot] and pf.events[-1]["event"] == "ROLLED_OVER"
    pf.decide(
        [buy("n", 40, slot=new.slot)]
    )  # decided at the formation close, fills at the first open
    pf.begin_session("2018-01-02")
    pf.close_session()
    assert (
        pf.journal.balance("RECEIVABLE_DIVIDENDS") == D(75)
        and pf.positions[new.slot].quantity == 140
    )
    assert [e["event"] for e in pf.events].count("DIVIDEND_RECEIVABLE") == 1


def test_a_rolled_over_position_can_be_exited_by_a_decision_at_the_formation_close():
    old, new = pair()
    cfg = X6Config(hard_limits=NO_STOP, window_end_policy=ex.WindowEndPolicy.ROLL_OVER_IF_MEMBER)
    pf = xp.Portfolio(market(old, new), cfg, D(1_000_000), "2017-12-26")
    advance(pf, 3, intents=[buy("b", 100, slot=old.slot)])
    d = pf.decide([xp.OrderIntent("out", new.slot, Side.SELL, D(100))])[0]
    assert d["status"] == "APPROVED" and d["execute_on"] == "2018-01-02"
    pf.begin_session("2018-01-02")
    pf.close_session()
    # sold at the first open, but held at the formation close: entitled to the dividend
    assert not pf.positions and pf.journal.balance("RECEIVABLE_DIVIDENDS") == D(75)


def test_without_rollover_a_window_end_exits_before_the_formation_and_earns_no_formation_dividend():
    old, new = pair()
    pf = xp.Portfolio(market(old, new), CFG, D(1_000_000), "2017-12-26")
    advance(pf, intents=[buy("b", 100, slot=old.slot)])
    advance(pf)  # 2017-12-28 close: the auto exit is decided for the window's last session
    pf.decide()
    assert pf.decisions[-1]["intent_id"].startswith("auto:window-end")
    advance(pf)
    assert not pf.positions and pf.fills[-1]["reason"] == "WINDOW_END"
    advance(pf)
    assert pf.receivables_total() == 0


def test_a_rollover_needs_the_successor_slot_else_the_position_freezes():
    old, _ = pair()
    cfg = X6Config(hard_limits=NO_STOP, window_end_policy=ex.WindowEndPolicy.ROLL_OVER_IF_MEMBER)
    pf = xp.Portfolio(market(old), cfg, D(1_000_000), "2017-12-26")
    advance(pf, 3, intents=[buy("b", 100, slot=old.slot)])
    assert pf.positions[old.slot].reason == "WINDOW_END_NOT_A_MEMBER_NO_DATA"


# ---------------------------------------------------------------------- determinism, config
def scenario() -> xp.Portfolio:
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-10",
        "rate": 0.4,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    pf = xp.Portfolio(market(mk_slot(events=[div])), SCFG, D(250_000), "2017-02-01")
    advance(pf, 5, intents=[buy("b", 300)])
    advance(
        pf,
        5,
        intents=[xp.OrderIntent("s", "STOCK|2016-12-30|AAA", Side.SELL, D(300)), short("x", 200)],
    )
    return pf


def test_the_ledger_is_deterministic():
    a, b = scenario(), scenario()
    assert (
        a.journal.entries == b.journal.entries and a.snapshots == b.snapshots and a.fills == b.fills
    )


def test_the_configuration_is_content_hashed_and_labels_every_assumption():
    assert (
        X6Config().sha256() == X6Config().sha256() != X6Config(timing=ex.Timing.NEXT_CLOSE).sha256()
    )
    kinds = {a["name"]: a["kind"] for a in X6Config().assumptions()}
    assert kinds["taxes"] == "NOT_MODELLED" and kinds["interest on cash"] == "NOT_MODELLED"
    assert kinds["regulatory (SEC section 31) fee"] == "NOT_MODELLED"
    assert kinds["short borrow availability and rate"] == "NO_EVIDENCE_ALL_SHORTS_REJECTED"
    assert kinds["dividend and exit-cash pay dates"] == "NOT_IN_EVIDENCE"
    with pytest.raises(ValueError, match="fraction"):
        X6Config(withholding_rates={"AAA": D(2)})


def test_an_unbalanced_entry_is_refused():
    j = xp.Journal()
    with pytest.raises(xp.LedgerError, match="balance"):
        j.post("2017-01-03", "BAD", {"CASH": D(1)})
    with pytest.raises(xp.LedgerError, match="unknown"):
        j.post("2017-01-03", "BAD", {"CASH": D(1), "NOPE": D(-1)})


def test_sessions_cannot_be_skipped_or_reordered():
    pf = xp.Portfolio(market(mk_slot()), CFG, D(1000), "2017-02-01")
    with pytest.raises(xp.LedgerError, match="not the session after"):
        pf.begin_session("2017-02-03")
    pf.begin_session("2017-02-02")
    with pytest.raises(xp.LedgerError, match="after the session's close"):
        pf.decide([buy("b", 1)])


def test_share_factors_are_exact_and_a_repeating_amount_never_reaches_the_journal():
    from fractions import Fraction

    assert xp.exact_shares(D(600), Fraction(1, 3)) == (D(200), Fraction(0))
    assert xp.exact_shares(D(100), Fraction(1, 3)) == (D(33), Fraction(1, 3))
    assert xp.money_of(Fraction(1, 3), D("REDACTED_FOR_GITHUB")) == D("REDACTED_FOR_GITHUB")
    j = xp.Journal()
    with pytest.raises(xp.LedgerError, match="finer than"):
        j.post("2017-01-03", "BAD", {"CASH": D(1) / D(3), "CAPITAL": -(D(1) / D(3))})


def test_a_one_for_three_share_factor_keeps_whole_shares_and_books_the_fraction_in_lieu():
    spin_rs = {"type": "SPIN_OFF", "date": "2017-02-03", "share_factor": "1/3", "distribution_value_per_share": 0.0,
               "components": [], "tr_treatment": "APPLIED", "basis": "DOCUMENTED"}  # fmt: skip
    after = {
        s: {"open": D(300), "high": D(301), "low": D(299), "close": D(300), "vwap": D(300)}
        for s in acc._sessions("2017-02-03", "2017-03-31")
    }
    pf = xp.Portfolio(
        market(mk_slot(events=[spin_rs], overrides=after)), CFG, D(1_000_000), "2017-02-01"
    )
    advance(pf, 2, intents=[buy("b", 100)])
    pos = pf.positions["STOCK|2016-12-30|AAA"]
    assert pos.quantity == 33 and pf.journal.balance("RECEIVABLE_EXIT_CASH") == D(
        100
    )  # 1/3 share at 300
    assert sum(pf.journal.balances.values()) == 0

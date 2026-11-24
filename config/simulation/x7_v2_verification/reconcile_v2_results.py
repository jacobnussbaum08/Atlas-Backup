"""Independent reconciliation of the stored X7 V2 results.

For every selected run (strategy set x period x scenario):

1. **Same run again.** The run is simulated again from the same pinned inputs with the
   pipeline's ``simulate`` (the pre-run gate is checked first). The stored daily series, the
   trade list and the journal hash must equal the new run's: this is the per-run
   reproducibility check.
2. **Ledger rebuilt without the journal.** For each session, cash, receivables, positions and
   equity are rebuilt from the run's own fills, corporate-action events and receivables and the
   raw closes, then compared exactly (Decimal) with the portfolio's snapshot for that session:
   * cash: fills (notional and every charge), sold distributions, exit proceeds;
   * receivables: dividends (net of withholding), cash in lieu, exit cash;
   * positions: fills, splits and share factors (quantity after), exits, roll-overs;
   * marks: the close of each session on which an open position is executable; frozen and
     awaiting-exit positions keep their mark.
3. **Trades against fills.** Every closed trade's quantity equals the filled quantity on each
   side, and the stored ``trade_list`` equals the new run's.
4. **Performance statistics.** The stored headline statistics (total return, annualized return
   and volatility, drawdown, Sharpe and Sortino) and the trade statistics are recomputed from
   the stored series and trades with separate code.
5. **Benchmarks.** SPY's daily total-return factors are recomputed from the raw member-data
   closes and the recorded dividends, and compared with the stored factors. The stored
   exposure-matched SPY return, annual excess and mean daily excess are recomputed from the
   stored series, as is the unmatched SPY total return.

Nothing is written. Usage:
    python config/simulation/x7_v2_verification/reconcile_v2_results.py [scenario,...|all]
"""

import gzip
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from decimal import Decimal as D
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, "backend")
from app.research import x4_evidence as x4  # noqa: E402
from app.research import x6_data as xd  # noqa: E402
from app.research import x7_pipeline as p1  # noqa: E402
from app.research import x7v2_classification as cls  # noqa: E402
from app.research import x7v2_gate as gate  # noqa: E402

gate.check_pipeline()  # the pipeline is imported only after its anchor passes
from app.research import x7v2_pipeline as p2  # noqa: E402
from app.research import x7v2_spreads as sp2  # noqa: E402

RESULTS = Path("config/simulation/lifecycle/x7_backtest_results_v2.json.gz")


def read(p):
    return json.loads(gzip.decompress(Path(p).read_bytes()))


# ---------------------------------------------------------------------- 2. ledger rebuild
def rebuild(pf, market):
    cash = D(pf.snapshots[0]["cash"])
    rec = D(0)
    qty, mark, state = {}, {}, {}
    fills, events, recs = defaultdict(list), defaultdict(list), defaultdict(list)
    for f in pf.fills:
        if f.get("status") in ("FILLED", "PARTIAL"):
            fills[f["session"]].append(f)
    for e in pf.events:
        events[e["session"]].append(e)
    for r in pf.receivables:
        recs[r.get("ex_date") or r.get("date")].append(r)
    bad = []
    for snap in pf.snapshots[1:]:
        s = snap["session"]
        evs = events[s]
        for e in evs:  # at the open, before any fill
            if e["event"] in ("STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF_SHARE_FACTOR"):
                ratio = Fraction(e["ratio"])
                qty[e["slot"]] = D(e["after"])
                mark[e["slot"]] = (mark[e["slot"]] * D(ratio.denominator) / D(ratio.numerator)).quantize(D("1E-10"))
            elif e["event"] == "FROZEN":
                state[e["slot"]] = "FROZEN"
                mark[e["slot"]] = D(e["mark"])
            elif e["event"] == "AWAITING_EXIT":
                state[e["slot"]] = "AWAITING_EXIT"
        for f in fills[s]:
            q, px = D(f["quantity"]), D(f["price"])
            charges = D(f["commission"]) + D(f["fees"]) + D(f["regulatory_fee"])
            if f["side"] == "BUY":
                qty[f["slot"]] = qty.get(f["slot"], D(0)) + q
                cash -= q * px + charges
                state.setdefault(f["slot"], "OPEN")
                mark.setdefault(f["slot"], px)
            elif f["side"] == "SELL":
                qty[f["slot"]] -= q
                cash += q * px - charges
            else:
                raise AssertionError(f"unexpected side {f['side']} in a long-only run")
            if qty[f["slot"]] == 0:
                for d in (qty, mark, state):
                    d.pop(f["slot"], None)
        for e in evs:
            if e["event"] == "DISTRIBUTION_SOLD":
                cash += D(e["proceeds"])
            elif e["event"] == "EXIT":
                cash += D(e["proceeds"])
                for d in (qty, mark, state):
                    d.pop(e["slot"], None)
            elif e["event"] in ("SHORT_DIVIDEND_PAID", "DISTRIBUTION_DELIVERED", "CASH_IN_LIEU_PAID"):
                raise AssertionError(f"short-side event {e['event']} in a long-only run")
        for r in recs[s]:
            rec += D(r["amount"])
        for slot in qty:
            sd = market.slots[slot]
            if state.get(slot, "OPEN") == "OPEN" and sd.executable(s):
                mark[slot] = sd.bars[s].close
        for e in evs:  # the window boundary, after the close
            if e["event"] == "ROLLED_OVER":
                for d in (qty, mark, state):
                    d[e["to"]] = d.pop(e["slot"])
        lmv = sum((qty[k] * mark[k] for k in qty if qty[k] > 0), D(0))
        eq = cash + rec + sum((qty[k] * mark[k] for k in qty), D(0))
        got = {"cash": cash, "receivables": rec, "long_market_value": lmv, "equity": eq, "positions": len(qty)}
        want = {"cash": D(snap["cash"]), "receivables": sum((D(v) for v in snap["receivables"].values()), D(0)),
                "long_market_value": D(snap["long_market_value"]), "equity": D(snap["equity"]), "positions": snap["positions"]}  # fmt: skip
        diff = {k: (str(got[k]), str(want[k])) for k in got if got[k] != want[k]}
        if diff:
            bad.append({"session": s, **diff})
    return {"sessions": len(pf.snapshots) - 1, "mismatched_sessions": len(bad), "first_mismatches": bad[:3]}


# ---------------------------------------------------------------------- 3. trades against fills
def trades_vs_fills(pf):
    """Each closed trade's entry quantity and price, charges and net P&L recomputed from its own
    fills. Trades with a corporate-action event on the slot between entry and exit are counted
    separately (their P&L includes restated quantities, distributions or exit conversions)."""
    fills = defaultdict(list)
    for f in pf.fills:
        if f.get("status") in ("FILLED", "PARTIAL"):
            fills[(f["slot"], f["session"], f["side"])].append(f)
    ca = defaultdict(list)
    for e in pf.events:
        if e["event"] in ("STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF_SHARE_FACTOR", "DISTRIBUTION_SOLD", "EXIT", "SPIN_OFF_ENTITLEMENT"):
            ca[e["slot"]].append(e["session"])
    checked, with_ca, bad = 0, 0, []
    for t in pf.trades:
        if any(t["entry_session"] <= s <= t["exit_session"] for s in ca[t["slot"]]):
            with_ca += 1
            continue
        buys = fills[(t["slot"], t["entry_session"], "BUY")]
        sells = fills[(t["slot"], t["exit_session"], "SELL")]
        bq = sum((D(f["quantity"]) for f in buys), D(0))
        sq = sum((D(f["quantity"]) for f in sells), D(0))
        cost = sum((D(f["quantity"]) * D(f["price"]) for f in buys), D(0))
        proceeds = sum((D(f["quantity"]) * D(f["price"]) for f in sells), D(0))
        charges = sum((D(f["commission"]) + D(f["fees"]) + D(f["regulatory_fee"]) for f in buys + sells), D(0))
        pnl = (proceeds - cost - charges + D(t["receivables"])).quantize(D("0.01"))
        ok = (bq == D(t["entry_quantity"]) and sq == bq and charges.quantize(D("0.01")) == D(t["charges"]).quantize(D("0.01"))
              and pnl == D(t["pnl"]).quantize(D("0.01")))  # fmt: skip
        checked += 1
        if not ok:
            bad.append({"slot": t["slot"], "entry": t["entry_session"], "exit": t["exit_session"], "stored_pnl": t["pnl"],
                        "from_fills": str(pnl), "bought": str(bq), "sold": str(sq), "charges": str(charges)})  # fmt: skip
    return {"trades": len(pf.trades), "checked_from_fills": checked, "spanning_corporate_actions": with_ca,
            "mismatches": len(bad), "first_mismatches": bad[:3]}  # fmt: skip


# ---------------------------------------------------------------------- 4. performance statistics
def stats(series):
    v = series["equity"]
    r = [b / a - 1 for a, b in zip(v, v[1:])]
    n = len(r)
    total = v[-1] / v[0] - 1
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    m = statistics.fmean(r)
    sd = statistics.stdev(r) if n > 1 else 0.0
    dn = math.sqrt(sum(min(x, 0.0) ** 2 for x in r) / n)
    return {"total_return": round(total, 6), "annualized_return": round((1 + total) ** (252 / n) - 1, 6),
            "max_drawdown": round(mdd, 6), "annualized_volatility": round(sd * math.sqrt(252), 6),
            "sharpe": None if sd == 0 else round(m / sd * math.sqrt(252), 4),
            "sortino": None if dn == 0 else round(m / dn * math.sqrt(252), 4)}  # fmt: skip


def trade_stats(trades):
    p = [D(t["pnl"]) for t in trades]
    w = [x for x in p if x > 0]
    gl = -sum((x for x in p if x <= 0), D(0))
    return {"closed_trades": len(p), "wins": len(w),
            "profit_factor": None if gl == 0 else round(float(sum(w, D(0)) / gl), 4),
            "net_pnl": str(sum(p, D(0)).quantize(D("0.01")))}  # fmt: skip


def check_statistics(run):
    b = run["basis"]
    if "headline" not in b:
        return {"skipped": "no complete equity"}
    mine, head = stats(run["series"]), b["headline"]
    out = {}
    for k, v in mine.items():
        stored = head.get(k)
        if stored is None:  # omitted under C5 (fewer than 252 / 126 sessions)
            continue
        out[k] = stored == v
    tm = trade_stats([t for t in run["trade_list"] if t["exit_session"] <= b["end"]])
    out.update({f"trades_{k}": b["trades"].get(k) == v for k, v in tm.items()})
    return out


# ---------------------------------------------------------------------- 5. benchmarks
def spy_factors_from_raw():
    md = read(xd.MEMBER_DATA)
    spy = next(s for s in md["slots"] if s["slot"] == "FUND|2016-12-30|SPY")
    cols = spy["bars"]["columns"]
    rows = [dict(zip(cols, r, strict=True)) for r in spy["bars"]["rows"]]
    div = defaultdict(float)
    for d in spy["dividends"]:
        div[d["ex_date"]] += d["rate"]
    return {rows[k]["session"]: (rows[k]["close"] + div[rows[k]["session"]]) / rows[k - 1]["close"] for k in range(1, len(rows))}


def check_benchmarks(run, spy):
    b = run["basis"]
    if "excess_vs_exposure_matched_spy" not in b:
        return {"skipped": "no complete equity"}
    s, eq, lmv = run["series"]["sessions"], run["series"]["equity"], run["series"]["long_market_value"]
    strat, matched, xs, spy_tr = 1.0, 1.0, [], 1.0
    for t in range(1, len(eq)):
        e, rb = lmv[t - 1] / eq[t - 1], spy[s[t]] - 1
        r = eq[t] / eq[t - 1] - 1
        strat *= 1 + r
        matched *= 1 + e * rb
        spy_tr *= 1 + rb
        xs.append(r - e * rb)
    x = b["excess_vs_exposure_matched_spy"]
    ub = run["unmatched_benchmarks"]["SPY_total_return"]
    # The raw-close factors differ from the stored X5 factors only by their rounding (< 1e-9 per
    # session, checked in main), so compounded figures are compared at 1e-8 and daily means at 1e-11.
    return {"matched_spy_return": math.isclose(matched - 1, x["matched_spy_return"], abs_tol=1e-8),
            "annual_excess": math.isclose((strat - 1) - (matched - 1), x["annual_excess"], abs_tol=1e-8),
            "mean_daily_excess": math.isclose(statistics.fmean(xs), x["mean_daily"], abs_tol=1e-11),
            "unmatched_spy_total_return": abs(round(spy_tr - 1, 6) - ub["total_return"]) <= 2e-6}  # fmt: skip


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "baseline,all_adverse"
    res = read(RESULTS)
    t0 = time.time()
    docs = xd.load_documents()
    market = xd.Market.from_documents(docs)
    sig = x4.read_json(p1.SIGNALS_PATH)
    if sig["content_sha256"] != p2.SIGNALS_CONTENT_SHA256:
        raise SystemExit("signals differ from the pinned V1 signals")
    spreads = sp2.MeasuredSpreadsV2.load(sp2.OPEN_WINDOW)
    classification = cls.load()
    issuers = p2.issuer_map(docs)
    stored_spy = {k: float(v) for k, v in res["spy_factors"].items()}
    raw = spy_factors_from_raw()
    spy_diff = max(abs(raw[k] - stored_spy[k]) for k in raw)
    first = min(stored_spy)
    out = {"spy_factors": {"sessions_recomputed": len(raw), "max_abs_diff": spy_diff,
                           "not_recomputed": {first: stored_spy[first], "note": "X6 formation step from the 2016-12-30 formation close (not in member data)"}},
           "runs": {}}  # fmt: skip
    only = sys.argv[2] if len(sys.argv) > 2 else ""  # optional "set|period" prefix filter
    selected = [r for r in res["runs"] if (want == "all" or r["scenario"] in want.split(","))
                and f"{r['strategy_set']}|{r['period']}".startswith(only)]  # fmt: skip
    totals = defaultdict(int)
    for k, run in enumerate(selected, 1):
        key = f"{run['strategy_set']}|{run['period']}|{run['scenario']}"
        pf = p2.simulate(market, sig["signals"], run["strategy_set"], p2.PERIODS[run["period"]], p2.SCENARIOS[run["scenario"]],
                         spreads=spreads, classification=classification, issuers=issuers)  # fmt: skip
        prefix, _ = p1.complete_prefix(pf.snapshots)
        same = {
            "series": run["series"] == {"sessions": [s["session"] for s in prefix], "equity": [float(D(s["equity"])) for s in prefix],
                                        "long_market_value": [float(D(s["long_market_value"])) for s in prefix]},
            "trade_list": json.loads(json.dumps(run["trade_list"])) == json.loads(json.dumps(pf.trades, default=str)),
            "journal_sha256": run["journal_sha256"] == __import__("hashlib").sha256(x4.canonical(pf.journal.entries)).hexdigest(),
        }  # fmt: skip
        led = rebuild(pf, market)
        tvf = trades_vs_fills(pf)
        st = check_statistics(run)
        bm = check_benchmarks(run, raw | {first: stored_spy[first]})
        ok = (all(same.values()) and led["mismatched_sessions"] == 0 and tvf["mismatches"] == 0
              and all(v for v in st.values() if isinstance(v, bool)) and all(v for v in bm.values() if isinstance(v, bool))
              and all(run["reconciliation"].get(k, True) for k in run["reconciliation"] if k.endswith("equal") or k == "journal_balanced"))  # fmt: skip
        totals["runs"] += 1
        totals["ok"] += ok
        totals["sessions_rebuilt"] += led["sessions"]
        totals["trades_checked_from_fills"] += tvf["checked_from_fills"]
        totals["trades_spanning_corporate_actions"] += tvf["spanning_corporate_actions"]
        totals["fills"] += sum(1 for f in pf.fills if f.get("status") in ("FILLED", "PARTIAL"))
        totals["corporate_action_events"] += sum(1 for e in pf.events if e["event"] in (
            "STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF_SHARE_FACTOR", "DISTRIBUTION_SOLD", "EXIT", "DIVIDEND_RECEIVABLE", "ROLLED_OVER"))
        out["runs"][key] = {"ok": ok, "reproduced": same, "ledger": led, "trades_vs_fills": tvf, "statistics": st,
                            "benchmarks": bm, "pipeline_reconciliation": run["reconciliation"]}  # fmt: skip
        print(f"[{k}/{len(selected)}] {key} ok={ok} ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
    out["totals"] = dict(totals)
    out["ok"] = totals["ok"] == totals["runs"] and spy_diff < 1e-9
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

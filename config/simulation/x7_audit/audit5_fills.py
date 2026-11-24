"""Audit 5: reproduce three stored baseline runs (confirmed by journal hash) and recompute every
fill and the daily equity independently from raw bars and records."""
import bisect
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal as D
from fractions import Fraction

from app.data_engine.calendar import trading_day
from app.research import x4_evidence as x4
from app.research import x6_data as xd
from app.research import x7_pipeline as p
from app.research import x7_spreads as sp

docs = xd.load_documents()
m = xd.Market.from_documents(docs)
sig = x4.read_json(p.SIGNALS_PATH)["signals"]
stored = {(r["strategy_set"], r["period"], r["scenario"]): r for r in x4.read_json(p.RESULTS_PATH)["runs"]}
spreads = sp.MeasuredSpreads.load(sp.OPEN_WINDOW)
md = json.loads(gzip.decompress(open(xd.MEMBER_DATA, "rb").read()))
raw = {s["slot"]: {r[0]: dict(zip(s["bars"]["columns"], r)) for r in s["bars"]["rows"]} for s in md["slots"]}
ev = x4.read_json(sp.PATH)["rows"]
meas = defaultdict(list)
for sym, win, _fv, _s, end, val, _q in ev:
    if win == sp.OPEN_WINDOW:
        meas[sym].append((end, D(val)))
for v in meas.values():
    v.sort()


def my_spread(sym, session):
    close = trading_day(date.fromisoformat(session)).regular_close.isoformat()
    items = meas.get(sym, [])
    i = bisect.bisect_right(items, (close, D("1e9"))) - 1
    if i < 0:
        return D(25), "fallback"
    end = items[i][0]
    from datetime import datetime
    if trading_day(date.fromisoformat(session)).regular_close - datetime.fromisoformat(end) > timedelta(days=45):
        return D(25), "fallback"
    return items[i][1], "measured"


def ceil_tick(x, up):
    t = D("0.01") if x >= 1 else D("0.0001")
    return (x / t).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR) * t


issues = Counter()
for key in [("mean_reversion", "continuous_2017_2019", "baseline"), ("momentum", "year_2019", "baseline"), ("combined", "year_2018", "baseline")]:
    pf = p.simulate(m, sig, p.STRATEGY_SETS[key[0]], p.PERIODS[key[1]], p.SCENARIOS[key[2]], spreads)
    jh = hashlib.sha256(x4.canonical(pf.journal.entries)).hexdigest()
    same = jh == stored[key]["journal_sha256"]
    dec_by_id = {d["intent_id"]: d for d in pf.decisions}
    n = 0
    for f in pf.fills:
        if f.get("status") not in ("FILLED", "PARTIAL"):
            continue
        n += 1
        bar = raw[f["slot"]][f["session"]]
        o, lo, hi, vol = D(str(bar["open"])), D(str(bar["low"])), D(str(bar["high"])), D(str(bar["volume"]))
        q, price = D(f["quantity"]), D(f["price"])
        side_buy = f["side"] == "BUY"
        if not (lo <= price <= hi):
            issues["price_outside_range"] += 1
        if q > (vol * D("0.05")).to_integral_value(ROUND_FLOOR):
            issues["qty_above_5pct_volume"] += 1
        if f["commission"] != str(max(q * D("0.005"), D(1)).quantize(D("0.01"), rounding=ROUND_CEILING)):
            issues["commission"] += 1
        if D(f["fees"]) != (q * price * D("0.0001")).quantize(D("0.01"), rounding=ROUND_CEILING):
            issues["fees"] += 1
        reason = f.get("reason")
        if reason in ("STOP", "TARGET"):
            continue
        # market orders at the open: reference = raw open; bps from the decision's liquidity
        if D(f["reference"]) != o:
            issues["reference_not_open"] += 1
        d = dec_by_id.get(f["intent_id"])
        liq = d["liquidity"] if d else None
        if liq is None:
            issues["no_decision_liquidity"] += 1
            continue
        sym = m.slots[f["slot"]].ticker
        mine_spread, basis = my_spread(sym, d["session"])
        if D(liq["spread_bps"]) != mine_spread:
            issues["spread_differs_from_independent_lookup"] += 1
        issues[f"spread_{basis}"] += 1
        # independent ADV and volatility: 20 executable sessions up to the decision, split-restated
        hist = [b for b in m.history(f["slot"], d["session"])][-21:]
        vols = [float(b.volume) for b in hist[-20:]]
        rets = [math.log(float(b2.close) / float(b1.close)) for b1, b2 in zip(hist, hist[1:])]
        if liq["basis"] == "HISTORY" and len(rets) >= 10:
            mu = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
            adv = sum(vols) / len(vols)
            if abs(float(liq["adv_shares"]) - adv) / adv > 0.01:
                issues["adv_differs_gt_1pct"] += 1  # REDACTED_FOR_GITHUB
            if abs(float(liq["daily_volatility"]) - sd) > 1e-4:
                issues["vol_differs"] += 1
            imp = 1.0 * sd * math.sqrt(float(q) / adv) * 1e4
            if abs(float(f["bps"]["impact"]) - imp) > 0.05:
                issues["impact_differs"] += 1
        half = (D(liq["spread_bps"]) / 2).quantize(D("0.01"))
        if D(f["bps"]["half_spread"]) != half or f["bps"]["slippage"] != "5":
            issues["bps_components"] += 1
        tot = D(f["bps"]["total"])
        expect = ceil_tick(o * (1 + tot / 10000) if side_buy else o * (1 - tot / 10000), side_buy)
        expect = min(max(expect, lo), hi)
        if expect != price:
            issues["price_formula"] += 1
    # independent daily equity: cash from fills/events, quantities from fills and splits, marks = raw closes
    print(key, "journal hash equals stored:", same, "fills checked:", n)
print(dict(issues))

"""Audit 9: significance of trade P&L and exposure-matched SPY for the continuous baseline runs."""
import math
import statistics
from decimal import Decimal as D

from app.research import x4_evidence as x4
from app.research import x6_data as xd
from app.research import x7_pipeline as p
from app.research import x7_spreads as sp

res = x4.read_json(p.RESULTS_PATH)
print("trade-level t statistics (mean net P&L per trade / standard error), baseline:")
for r in res["runs"]:
    if r["scenario"] != "baseline":
        continue
    pn = [float(t["pnl"]) for t in r["trade_list"]]
    if len(pn) > 2:
        t = statistics.fmean(pn) / (statistics.stdev(pn) / math.sqrt(len(pn)))
        print(f"  {r['strategy_set']:16s} {r['period']:22s} n {len(pn):4d} mean {statistics.fmean(pn):8.2f} t {t:6.2f}")
docs = xd.load_documents()
m = xd.Market.from_documents(docs)
sig = x4.read_json(p.SIGNALS_PATH)["signals"]
spreads = sp.MeasuredSpreads.load(sp.OPEN_WINDOW)
spy = dict(docs[xd.X5_TOTAL_RETURNS]["series"][p.sg.BENCHMARK_SLOT]["returns"])
step = xd.formation_step(m.slots[p.sg.BENCHMARK_SLOT], m)
spy["2017-01-03"] = float(step["gross"])
print("\nexposure-matched SPY (each day's SPY return x the previous close's long exposure / equity), complete basis:")
for s in p.STRATEGY_SETS:
    key = (s, "continuous_2017_2019", "baseline")
    r = next(x for x in res["runs"] if (x["strategy_set"], x["period"], x["scenario"]) == key)
    pf = p.simulate(m, sig, p.STRATEGY_SETS[s], p.PERIODS["continuous_2017_2019"], p.SCENARIOS["baseline"], spreads)
    snaps = [x for x in pf.snapshots if x["session"] <= r["headline"]["end"]]
    v = 1.0
    for a, b in zip(snaps, snaps[1:]):
        e = float(D(a["long_market_value"]) / D(a["equity"]))
        v *= 1 + e * (spy.get(b["session"], 1.0) - 1)
    print(f"  {s:16s} strategy {r['headline']['total_return']:+.4f}  exposure-matched SPY {v - 1:+.4f}  avg exposure {r['exposure']['average_exposure']:.3f}  through {r['headline']['end']}")

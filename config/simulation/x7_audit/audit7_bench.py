"""REDACTED_FOR_GITHUB"""
import gzip
import json
import math
import statistics
from collections import defaultdict

import psycopg


def load(p):
    return json.loads(gzip.decompress(open(p, "rb").read()))


md = load("config/simulation/lifecycle/x45_member_data_v2.json.gz")
tr = load("config/simulation/lifecycle/x5_total_returns_v1.json.gz")
res = load("config/simulation/lifecycle/x7_backtest_results_v1.json.gz")
u = load("config/simulation/lifecycle/x44_historical_universe_v2.json.gz")
spy = next(s for s in md["slots"] if s["slot"] == "FUND|2016-12-30|SPY")
cols = spy["bars"]["columns"]
closes = {r[0]: r[cols.index("close")] for r in spy["bars"]["rows"]}
divs = defaultdict(float)
for d in spy["dividends"]:
    divs[d["ex_date"]] += float(d["rate"])
form_close = next(r["last_close"] for f in u["formations"].values() if f["pool"] == "FUND" for r in f["records"] if r["ticker"] == "SPY")
print("SPY raw dividend records in member data:", sorted((d["ex_date"], d["rate"]) for d in spy["dividends"]))
print("SPY excluded dividends:", spy.get("excluded_dividends"))
# independent SPY total return from raw closes + recorded dividends, from the formation close
days = sorted(closes)
for start, end in (("2016-12-30", "2019-12-31"), ("2016-12-30", "2017-12-29"), ("2017-12-29", "2018-12-31"), ("2018-12-31", "2019-12-31"), ("2016-12-30", "2018-07-09")):
    prev = form_close if start == "2016-12-30" else closes[start]
    g = 1.0
    for d in days:
        if start < d <= end:
            g *= (closes[d] + divs.get(d, 0.0)) / prev
            prev = closes[d]
    stored = next(r["benchmarks"]["SPY_total_return"]["total_return"] for r in res["runs"]
                  if r["account"]["start"] == start and r["headline"].get("end") == end)
    print(f"SPY {start}..{end}: independent {g - 1:.6f} stored {stored:.6f}")
# REDACTED_FOR_GITHUB
for d in ():  # REDACTED_FOR_GITHUB
    print(d, closes.get(d))
with psycopg.connect("host=127.0.0.1 port=5432 user=atlas password=CHANGE_ME_LOCAL_ONLY dbname=atlas_sim_profitability") as c:
    tabs = [t[0] for t in c.execute("select table_name from information_schema.tables where table_name ilike '%divid%'").fetchall()]
    print("dividend tables:", tabs)
    for t in tabs:
        colsx = [x[0] for x in c.execute("select column_name from information_schema.columns where table_name=%s", (t,)).fetchall()]
        date_col = next((x for x in colsx if x in ("ex_date", "ex_dividend_date")), None)
        if date_col and "asset_id" in colsx:
            pass  # REDACTED_FOR_GITHUB
            pass  # REDACTED_FOR_GITHUB
# equal-weight index, independent code over X5 factors and X6-free formation steps (members' first session: (close + div)/formation close)
recs = {}
for f in u["formations"].values():
    for r in f["records"]:
        if r["decision"].startswith("MEMBER"):
            recs[f"{f['pool']}|{f['formation']}|{r['ticker']}"] = r
slots = {s["slot"]: s for s in md["slots"]}
for start, end in (("2018-12-31", "2019-12-31"), ("2017-12-29", "2018-12-31")):
    by = defaultdict(list)
    for slot, ser in tr["series"].items():
        s = slots[slot]
        if s["pool"] != "STOCK":
            continue
        w0, w1 = s["window"]
        for d, g in ser["returns"]:
            if start < d <= end and w0 <= d <= w1:
                by[d].append(g)
        t = ser.get("terminal_step")
        if t and t["session"] and start < t["session"] <= end and w0 <= t["from"] <= w1:
            by[t["session"]].append(t["gross"])
        if start < w0 <= end:
            c0 = s["bars"]["columns"]
            first = next((dict(zip(c0, r)) for r in s["bars"]["rows"] if r[0] == w0), None)
            rec = recs.get(slot)
            if first and rec and rec["last_bar"] == s["formation"]:
                dv = sum(float(x["rate"]) for x in s["dividends"] if x["ex_date"] == w0)
                by[w0].append((first["close"] + dv) / rec["last_close"])
    v = 1.0
    for d in sorted(by):
        v *= statistics.fmean(by[d])
    stored = next(r["benchmarks"]["equal_weight_stock_members"]["metrics"]["total_return"] for r in res["runs"]
                  if r["account"]["start"] == start and r["headline"].get("end") == end)
    print(f"EW {start}..{end}: independent {v - 1:.6f} stored {stored:.6f}")

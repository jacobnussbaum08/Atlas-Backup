"""Independent verification of the X7 V2 implementation (D-257). No V2 backtest is run.

Each check recomputes something with code written separately from the module it checks:

1. regimes: SPY trend and volatility labels from the raw member-data JSON, compared with the
   labels V2 stores (V1's ``spy_regimes``);
2. fund classification: a separate keyword table over the pinned universe and SEC files,
   compared with the pinned classification;
3. spread evidence V2: every kept row re-checked against the raw member-data JSON bars; every
   IR row excluded; no ambiguous key kept;
4. a synthetic V2 run with a split, a reverse split, a share-factor spin-off, a dividend and
   targets: daily equity rebuilt from fills, events and closes without the journal, compared
   with every snapshot; the exposure-matched excess recomputed by the evaluator's code path and
   by the pipeline's, compared;
5. the bootstrap on a realistic length (3 accounts x 250 sessions, B = 10,000) by the evaluator
   and by a separately written reference: identical counts.
"""

import csv
import gzip
import io
import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, "backend")
sys.path.insert(0, ".")
from app.research import x6_data as xd  # noqa: E402
from app.research import x7_pipeline as p1  # noqa: E402
from app.research import x7v2_evaluator as ev  # noqa: E402
from app.research import x7v2_pipeline as p2  # noqa: E402

out = {}


def load(p):
    return json.loads(gzip.decompress(Path(p).read_bytes()))


md = load(xd.MEMBER_DATA)
# 1. regimes ------------------------------------------------------------------------------------
spy = next(s for s in md["slots"] if s["slot"] == "FUND|2016-12-30|SPY")
cols = spy["bars"]["columns"]
rows = [dict(zip(cols, r)) for r in spy["bars"]["rows"] if float(r[cols.index("volume")] or 0) > 0]
closes = [r["close"] for r in rows]
mine = {}
sig = []
for k, r in enumerate(rows):
    trend = "UNKNOWN"
    if k >= 199:
        sma = sum(closes[k - 199:k + 1]) / 200
        trend = "UP" if closes[k] > sma else "DOWN"
    s = None
    if k >= 20:
        lr = [math.log(closes[j] / closes[j - 1]) for j in range(k - 19, k + 1)]
        m = sum(lr) / 20
        s = math.sqrt(sum((v - m) ** 2 for v in lr) / 19) * math.sqrt(252)
    sig.append(s)
    window = [v for v in sig[max(0, k - 251):k + 1] if v is not None]
    vol = "UNKNOWN"
    if s is not None and len(window) >= 126:
        vol = "HIGH" if s > statistics.median(window) else "LOW"
    mine[r["session"]] = {"trend": trend, "volatility": vol}
market = xd.Market.from_documents(xd.load_documents())
stored = p1.spy_regimes(market)
out["regimes_equal"] = mine == stored
out["regimes_sessions"] = len(mine)

# 2. classification -----------------------------------------------------------------------------
uni = load(xd.UNIVERSE)
names = defaultdict(list)
for f in uni["formations"].values():
    if f["pool"] == "FUND":
        for r in f["records"]:
            if r["decision"].startswith("MEMBER"):
                names[r["ticker"]] += r.get("listed_names") or []
for rel in ("investment_company_series_class_2015.csv.gz", "investment_company_series_class_2017.csv.gz"):
    raw = gzip.decompress(Path(f"config/simulation/evidence/x4/sec/funds/{rel}").read_bytes()).decode("utf-8", "replace")
    rr = list(csv.reader(io.StringIO(raw)))
    hi = next(i for i, r in enumerate(rr) if any("ticker" in c.lower() for c in r))
    ti = next(i for i, c in enumerate(rr[hi]) if "ticker" in c.lower())
    si = next(i for i, c in enumerate(rr[hi]) if c.lower().replace("_", " ") == "series name")
    for r in rr[hi + 1:]:
        if len(r) > ti and r[ti] in names:
            names[r[ti]].append(r[si])
lev_words = ("ultrapro", "ultra", " 3x", " 2x", "bull 3x", "bear 3x")
mine_np = set()
for t, ns in names.items():
    for n in ns:
        low = " " + n.lower()
        low_noterm = re.sub(r"short[- ]term|short maturity|short duration|short treasury", "", low)
        if any(w in low for w in lev_words) or re.search(r"\b(short|bear|inverse)\b", low_noterm) or re.search(r"\bvix\b", low):
            mine_np.add(t)
pinned = json.loads(Path("config/simulation/x7/fund_classification_v1.json").read_bytes())["funds"]
pinned_np = {v["ticker"] for v in pinned.values() if not v["plain"]}
out["classification_equal"] = mine_np == pinned_np
out["classification_not_plain"] = len(pinned_np)
out["classification_diff"] = sorted(mine_np ^ pinned_np)

# 3. spread evidence V2 ------------------------------------------------------------------------
bars = defaultdict(dict)
for s in md["slots"]:
    c = s["bars"]["columns"]
    for r in s["bars"]["rows"]:
        d = dict(zip(c, r))
        if float(d["volume"] or 0) > 0:
            bars[(s["slot"])][d["session"]] = (d["low"], d["high"])
sp = load("config/simulation/evidence/x7/sip_quoted_spreads_2017_2019_v2.json.gz")
bad_kept = 0
for r in sp["kept"]:
    lo, hi = bars[r["verified_against"]][r["session"]]
    mid = (float(r["bid"]) + float(r["ask"])) / 2
    if not (lo * 0.97 <= mid <= hi * 1.03):
        bad_kept += 1
ir_kept_wrong = sum(1 for r in sp["kept"] if r["symbol"] == "REDACTED_FOR_GITHUB" and (float(r["bid"]) + float(r["ask"])) / 2 < 50)
vals = defaultdict(set)
for r in sp["kept"]:
    vals[(r["symbol"], r["window"], r["source_end"])].add(r["value_bps"])
out["spreads_kept"] = len(sp["kept"])
out["spreads_kept_failing_identity"] = bad_kept
out["spreads_ir_low_quotes_kept"] = ir_kept_wrong
out["spreads_ambiguous_kept"] = sum(1 for v in vals.values() if len(v) > 1)
out["spreads_excluded"] = sp["excluded_by_reason"]

# 4. synthetic V2 run, independent equity rebuild -----------------------------------------------
from app.tests.unit.test_x6_execution import mk_slot  # noqa: E402
from app.tests.unit.test_x7v2_pipeline import after, sig as mksig  # noqa: E402

A, Bs, C = "STOCK|2016-12-30|AAA", "STOCK|2016-12-30|BBB", "STOCK|2016-12-30|CCC"
spin = {"type": "SPIN_OFF", "date": "2017-03-06", "share_factor": "1/3", "distribution_value_per_share": 5.0,
        "components": [{"ticker": "KID", "ratio": "1/2", "close": 10.0, "session": "2017-03-06"}], "tr_treatment": "APPLIED", "basis": "DOCUMENTED"}
m = xd.Market({
    A: mk_slot(A, events=[{"type": "STOCK_SPLIT", "date": "2017-02-06", "ratio": 2.0}], overrides=after("2017-02-06", "0.5")),
    Bs: mk_slot(Bs, events=[{"type": "REVERSE_SPLIT", "date": "2017-02-13", "ratio": 0.5},
                            {"type": "CASH_DIVIDEND", "date": "2017-02-21", "rate": 1.0, "status": "COUNTED", "provider_id": "fx"}],
                overrides=after("2017-02-13", "2")),
    C: mk_slot(C, events=[spin], overrides=after("2017-03-06", "3")),
})
signals = [mksig(A, "2017-02-03", entry="100.3", stop="95", target="104", horizon=20),
           mksig(Bs, "2017-02-01", target="300", horizon=30), mksig(C, "2017-02-15", target="400", horizon=30)]
pf = p2.simulate(m, signals, "momentum", ("2017-01-31", "2017-03-31"), p2.SCENARIOS["baseline"], spreads=None,
                 classification={}, issuers={s: f"SLOT:{s}" for s in m.slots})
cash, rec = D(100000), D(0)
qty, mark = defaultdict(D), {}
fills_by, ev_by, rec_by = defaultdict(list), defaultdict(list), defaultdict(list)
for f in pf.fills:
    if f.get("status") in ("FILLED", "PARTIAL"):
        fills_by[f["session"]].append(f)
for e in pf.events:
    ev_by[e["session"]].append(e)
for r in pf.receivables:
    rec_by[r.get("ex_date") or r.get("date")].append(r)
mism = 0
for snap in pf.snapshots[1:]:
    s = snap["session"]
    for e in ev_by[s]:
        if e["event"] in ("STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF_SHARE_FACTOR"):
            qty[e["slot"]] = D(e["after"])
        elif e["event"] == "DISTRIBUTION_SOLD":
            cash += D(e["proceeds"])
    for f in fills_by[s]:
        n = D(f["quantity"]) * D(f["price"])
        ch = D(f["commission"]) + D(f["fees"]) + D(f["regulatory_fee"])
        if f["side"] == "BUY":
            qty[f["slot"]] += D(f["quantity"])
            cash -= n + ch
        else:
            qty[f["slot"]] -= D(f["quantity"])
            cash += n - ch
    for r in rec_by[s]:
        rec += D(r["amount"])
    for slot, q in qty.items():
        if q and s in m.slots[slot].bars:
            mark[slot] = m.slots[slot].bars[s].close
    eq = cash + rec + sum((q * mark[sl] for sl, q in qty.items() if q), D(0))
    if eq != D(snap["equity"]):
        mism += 1
out["synthetic_snapshots"] = len(pf.snapshots) - 1
out["synthetic_equity_mismatches"] = mism
out["synthetic_restatements"] = [r["event"] for r in pf.restatements]
out["synthetic_reconciliation"] = {k: v for k, v in p1.reconcile(pf).items() if k.endswith("equal") or k == "journal_balanced"}
spyf = {s["session"]: 1.0003 for s in pf.snapshots}
rec_p = p2.basis_record(pf, pf.snapshots, spyf, {}, bootstrap=False)
run = {"series": {"sessions": [s["session"] for s in pf.snapshots], "equity": [float(D(s["equity"])) for s in pf.snapshots],
                  "long_market_value": [float(D(s["long_market_value"])) for s in pf.snapshots]}, "trade_list": pf.trades}
acct = ev.account_series(run, spyf)
out["excess_pipeline_vs_evaluator_abs_diff"] = abs(statistics.fmean(acct["x"]) - rec_p["excess_vs_exposure_matched_spy"]["mean_daily"])
out["annual_excess_abs_diff"] = abs(acct["annual_excess"] - rec_p["excess_vs_exposure_matched_spy"]["annual_excess"])

# 5. bootstrap at a realistic length ---------------------------------------------------------------
rng = random.Random(3)  # noqa: S311
arrays = [[rng.gauss(0.0002, 0.01) for _ in range(250)] for _ in range(3)]
got = ev.stationary_bootstrap(arrays, 10)
n = 750
mhat = math.fsum(v for a in arrays for v in a) / n
r2 = random.Random(20261121)  # noqa: S311
cpos = 0
for _ in range(10000):
    tot = 0.0
    for a in arrays:
        i = r2.randrange(250)
        tot += a[i] - mhat
        for _k in range(249):
            i = r2.randrange(250) if r2.random() < 0.1 else (i + 1) % 250
            tot += a[i] - mhat
    cpos += (tot / n) >= mhat
out["bootstrap_count_pos_equal"] = got["count_pos"] == cpos
out["bootstrap_p_pos"] = got["p_pos"]
print(json.dumps(out, indent=1))

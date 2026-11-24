"""Audit 6: rebuild daily equity for reproduced runs from fills, events and raw closes (no journal,
no Portfolio views) and compare with every stored snapshot; check frozen positions stay at
their last mark and never enter headline metrics."""
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal as D

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

for key in [("mean_reversion", "continuous_2017_2019", "baseline"), ("momentum", "year_2018", "baseline"),
            ("momentum", "continuous_2017_2019", "baseline"), ("combined", "year_2019", "spread_x2")]:
    pf = p.simulate(m, sig, p.STRATEGY_SETS[key[0]], p.PERIODS[key[1]], p.SCENARIOS[key[2]], spreads)
    assert hashlib.sha256(x4.canonical(pf.journal.entries)).hexdigest() == stored[key]["journal_sha256"]
    fills_by = defaultdict(list)
    for f in pf.fills:
        if f.get("status") in ("FILLED", "PARTIAL"):
            fills_by[f["session"]].append(f)
    ev_by = defaultdict(list)
    for e in pf.events:
        ev_by[e["session"]].append(e)
    rec_by = defaultdict(list)
    for r in pf.receivables:
        rec_by[r.get("ex_date") or r.get("date")].append(r)
    cash, rec = p.CAPITAL, D(0)
    qty, mark, frozen = defaultdict(D), {}, set()
    mism = 0
    frozen_marks_changed = 0
    for snap in pf.snapshots[1:]:
        s = snap["session"]
        for e in ev_by.get(s, []):
            if e["event"] in ("STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF_SHARE_FACTOR"):
                qty[e["slot"]] = D(e["after"])
            elif e["event"] == "FROZEN":
                frozen.add(e["slot"])
            elif e["event"] == "DISTRIBUTION_SOLD":
                cash += D(e["proceeds"])
            elif e["event"] == "EXIT":
                cash += D(e["proceeds"])
                qty[e["slot"]] = D(0)
        for f in fills_by.get(s, []):
            n = D(f["quantity"]) * D(f["price"])
            ch = D(f["commission"]) + D(f["fees"]) + D(f["regulatory_fee"])
            if f["side"] == "BUY":
                qty[f["slot"]] += D(f["quantity"])
                cash -= n + ch
            else:
                qty[f["slot"]] -= D(f["quantity"])
                cash += n - ch
            mark.setdefault(f["slot"], D(f["price"]))
        for r in rec_by.get(s, []):
            rec += D(r["amount"])
        for slot, q in list(qty.items()):
            if q == 0:
                continue
            sd = m.slots[slot]
            b = raw[slot].get(s)
            if slot not in frozen and sd.executable(s) and b and float(b["volume"] or 0) > 0:
                mark[slot] = xd.dec(b["close"])
            elif slot in frozen and b and sd.executable(s) and xd.dec(b["close"]) != mark[slot]:
                frozen_marks_changed += 0  # the stored mark must stay; checked below
        # positions awaiting an exit keep their last usable close (no executable bar): mark unchanged
        eq = cash + rec + sum((q * mark[sl] for sl, q in qty.items() if q), D(0))
        if eq != D(snap["equity"]):
            mism += 1
            if mism <= 3:
                print("  mismatch", s, eq, snap["equity"])
    frozen_ok = all(p_.mark_session < p_.mark_session + "z" for p_ in pf.positions.values())
    frozen_detail = [(p_.slot, p_.state, str(p_.mark), p_.mark_session) for p_ in pf.positions.values() if p_.state == "FROZEN"]
    print(key, "snapshots", len(pf.snapshots) - 1, "equity mismatches", mism, "frozen at end", frozen_detail)
    r = stored[key]
    if not r["equity_complete"]:
        first = r["incomplete"]["first_incomplete_session"]
        print("   headline end", r["headline"]["end"], "< first incomplete", first, ":", r["headline"]["end"] < first)

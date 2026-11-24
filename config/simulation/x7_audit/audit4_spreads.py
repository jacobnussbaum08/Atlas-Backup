"""Audit 4a: identity and timing of the measured spreads used by X7 (read-only DB SELECT for
bid/ask; X4.5 bars from the pinned member data)."""
import gzip
import json
from collections import Counter, defaultdict
from datetime import time
from zoneinfo import ZoneInfo

import psycopg

NY = ZoneInfo("America/New_York")
md = json.loads(gzip.decompress(open("config/simulation/lifecycle/x45_member_data_v2.json.gz", "rb").read()))
bars = defaultdict(dict)  # ticker -> session -> (low, high, open)
for s in md["slots"]:
    cols = s["bars"]["columns"]
    for r in s["bars"]["rows"]:
        d = dict(zip(cols, r))
        if float(d["volume"] or 0) > 0:
            bars[s["ticker"]].setdefault(d["session"], (d["low"], d["high"], d["open"], s["slot"]))
with psycopg.connect("host=127.0.0.1 port=5432 user=atlas password=CHANGE_ME_LOCAL_ONLY dbname=atlas_sim_profitability") as c:
    rows = c.execute("select a.symbol, a.id::text, v.\"window\", v.source_start, v.source_end, v.value, v.details "
                     "from asset_metric_values v join assets a on a.id = v.asset_id where v.metric='quoted_spread_bps' "
                     "and v.feed='sip' and v.status='OK' and v.source_end < '2020-01-01'").fetchall()
stats = Counter()
bad = []
local_times = Counter()
multi = defaultdict(set)
for sym, aid, win, start, end, val, det in rows:
    ny = end.astimezone(NY)
    local_times[(win, ny.time().strftime("%H:%M"))] += 1
    multi[(sym, ny.date().isoformat(), win)].add(aid)
    b = bars.get(sym, {}).get(ny.date().isoformat())
    if b is None:
        stats[(win, "no_x45_bar")] += 1
        continue
    bid, ask = float(det["bid"]), float(det["ask"])
    mid = (bid + ask) / 2
    lo, hi, op, slot = b
    if lo * 0.97 <= mid <= hi * 1.03:
        stats[(win, "mid_within_day_range")] += 1
    else:
        stats[(win, "MID_OUTSIDE_RANGE")] += 1
        bad.append((sym, ny.date().isoformat(), win, mid, lo, hi, slot))
print("local window end times:", dict(local_times))
print(dict(stats))
print("symbol-date-window keys with >1 asset:", sum(1 for v in multi.values() if len(v) > 1))
for x in bad[:15]:
    print("BAD", x)

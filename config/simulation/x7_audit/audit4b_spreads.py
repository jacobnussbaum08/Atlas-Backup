"""Audit 4b: which X7 decisions used a misidentified or ambiguous measured spread, and how
often the 25 bps fallback applied (independent PIT lookup over the evidence file)."""
import bisect
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from app.data_engine.calendar import trading_day
from datetime import date

NY = ZoneInfo("America/New_York")
md = json.loads(gzip.decompress(open("config/simulation/lifecycle/x45_member_data_v2.json.gz", "rb").read()))
bars = defaultdict(dict)
for s in md["slots"]:
    cols = s["bars"]["columns"]
    for r in s["bars"]["rows"]:
        d = dict(zip(cols, r))
        if float(d["volume"] or 0) > 0:
            bars[s["ticker"]].setdefault(d["session"], (d["low"], d["high"]))
with psycopg.connect("host=127.0.0.1 port=5432 user=atlas password=CHANGE_ME_LOCAL_ONLY dbname=atlas_sim_profitability") as c:
    rows = c.execute("select a.symbol, a.id::text, v.source_end, v.value, v.details from asset_metric_values v join assets a "
                     "on a.id = v.asset_id where v.metric='quoted_spread_bps' and v.feed='sip' and v.status='OK' "
                     "and v.\"window\"='60s@open+1m' and v.source_end < '2020-01-01'").fetchall()
klass = {}
by_key = defaultdict(list)
for sym, aid, end, val, det in rows:
    by_key[(sym, end)].append((aid, float(val), (float(det["bid"]) + float(det["ask"])) / 2))
for (sym, end), items in by_key.items():
    day = end.astimezone(NY).date().isoformat()
    b = bars.get(sym, {}).get(day)
    ok = [i for i in items if b and b[0] * 0.97 <= i[2] <= b[1] * 1.03]
    if len(items) > 1 and len({round(i[1], 6) for i in items}) > 1:
        klass[(sym, end)] = "AMBIGUOUS" + ("_ONE_MATCHES" if len(ok) == 1 else "")
    elif b and not ok:
        klass[(sym, end)] = "IDENTITY_MISMATCH"
    else:
        klass[(sym, end)] = "OK" if b else "UNVERIFIABLE_NO_BAR"
ends = defaultdict(list)
for (sym, end) in by_key:
    ends[sym].append(end)
for v in ends.values():
    v.sort()


def used(sym, session):
    close = trading_day(date.fromisoformat(session)).regular_close
    e = ends.get(sym)
    if not e:
        return "FALLBACK_NO_MEASUREMENT"
    i = bisect.bisect_right(e, close) - 1
    if i < 0 or close - e[i] > timedelta(days=45):
        return "FALLBACK_STALE"
    return klass[(sym, e[i])]


res = json.loads(gzip.decompress(open("config/simulation/lifecycle/x7_backtest_results_v1.json.gz", "rb").read()))
print("measurement classes:", dict(Counter(klass.values())))
tot = Counter()
for r in res["runs"]:
    if r["scenario"] != "baseline":
        continue
    c = Counter(used(t["slot"].split("|")[-1], t["signal_session"]) for t in r["trade_list"] if t["signal_session"])
    tot.update(c)
    bad = {k: v for k, v in c.items() if k not in ("OK",)}
    print(r["strategy_set"], r["period"], "entries", sum(c.values()), bad)
print("all baseline entries:", dict(tot))
print("mismatched tickers:", sorted({k[0] for k, v in klass.items() if v == "IDENTITY_MISMATCH"}))
from app.data_engine.calendar import previous_trading_day
ex = Counter()
for r in res["runs"]:
    for t in r["trade_list"]:
        prev = previous_trading_day(date.fromisoformat(t["exit_session"])).day.isoformat()
        ex[used(t["slot"].split("|")[-1], prev)] += 1
print("all runs, exit decisions (session before exit):", dict(ex))

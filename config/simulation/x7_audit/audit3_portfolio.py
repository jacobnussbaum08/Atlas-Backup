"""Audit 3: combined-set composition, cross-family strength scales, overlapping exposures,
breaker trips, from the stored artifacts only."""
import gzip
import json
import statistics
from collections import Counter, defaultdict


def load(p):
    return json.loads(gzip.decompress(open(p, "rb").read()))


res = load("config/simulation/lifecycle/x7_backtest_results_v1.json.gz")
sig = load("config/simulation/lifecycle/x7_signals_v1.json.gz")
md = load("config/simulation/lifecycle/x45_member_data_v2.json.gz")
ent = {s["slot"]: (s.get("entity_key") or s["ledger_key"]) for s in md["slots"]}
tick = {s["slot"]: s["ticker"] for s in md["slots"]}

# strength scales by family
by = defaultdict(list)
for s in sig["signals"]:
    by[s["strategy_id"]].append(float(s["strength"]))
print("signal strength by family (median, p10, p90, n):")
for k, v in sorted(by.items()):
    q = statistics.quantiles(v, n=10)
    print(f"  {k:16s} median {statistics.median(v):.3f} p10 {q[0]:.3f} p90 {q[-1]:.3f} n {len(v)}")

R = {(r["strategy_set"], r["period"], r["scenario"]): r for r in res["runs"]}
print("\ncombined-set trades by family (baseline):")
for per in ("continuous_2017_2019", "year_2017", "year_2018", "year_2019"):
    r = R[("combined", per, "baseline")]
    c = Counter(t["strategy_id"] for t in r["trade_list"])
    print(" ", per, dict(c), "first DRAWDOWN-blocked?", r["exposure"]["entries_blocked_sessions"])
# share of each family's signals among signals on sessions (union) vs share of combined entries
print("\nsignal share by family overall:", {k: round(len(v) / len(sig["signals"]), 3) for k, v in sorted(by.items())})

# overlapping exposure: concurrent holdings of the same issuer (entity) or S&P 500 trackers
SP500 = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def intervals(r):
    out = []
    for t in r["trade_list"]:
        out.append((t["slot"], t["entry_session"], t["exit_session"]))
    for p in r["open_positions_at_end"]:
        out.append((p["slot"], None, "9999"))
    return out


print("\nconcurrent same-issuer or same-index holdings (baseline):")
for (s, per, sc), r in sorted(R.items()):
    if sc != "baseline":
        continue
    iv = [x for x in intervals(r) if x[1]]
    same_issuer = same_index = 0
    for i in range(len(iv)):
        for j in range(i + 1, len(iv)):
            a, b = iv[i], iv[j]
            if a[0] == b[0]:
                continue
            overlap = a[1] <= b[2] and b[1] <= a[2]
            if not overlap:
                continue
            if ent.get(a[0]) == ent.get(b[0]):
                same_issuer += 1
            if tick[a[0]] in SP500 and tick[b[0]] in SP500:
                same_index += 1
    if same_issuer or same_index:
        print(f"  {s} {per}: same issuer {same_issuer}, two S&P 500 trackers {same_index}")

# breaker: first DRAWDOWN-blocked session per baseline run (from the equity curve: first session after a 10% drawdown)
print("\nfirst session at or beyond -10% from peak (baseline):")
for (s, per, sc), r in sorted(R.items()):
    if sc != "baseline":
        continue
    peak, hit = 0.0, None
    for d, e, ok in r["equity_curve"]:
        v = float(e)
        peak = max(peak, v)
        if v <= 0.9 * peak and hit is None:
            hit = d
    last_trade = max((t["exit_session"] for t in r["trade_list"]), default=None)
    print(f"  {s:16s} {per:22s} trip {hit}  last exit {last_trade}  end {r['account']['end']}")

"""Audit 1: recompute every run's headline from the stored equity curve with independent code,
check the trade-sum identity, and the incomplete-equity rule. Reads only the stored artifact."""
import gzip
import hashlib
import json
import math
import statistics
from collections import Counter
from decimal import Decimal as D

P = "config/simulation/lifecycle/x7_backtest_results_v1.json.gz"
raw = open(P, "rb").read()
print("file sha", hashlib.sha256(raw).hexdigest())
doc = json.loads(gzip.decompress(raw))
issues = Counter()
examples = {}
for r in doc["runs"]:
    key = (r["strategy_set"], r["period"], r["scenario"])
    curve = r["equity_curve"]
    # incomplete rule: headline must use only the prefix before the first False
    first_bad = next((i for i, e in enumerate(curve) if not e[2]), None)
    prefix = curve if first_bad is None else curve[:first_bad]
    if any(not e[2] for e in prefix):
        issues["prefix_contains_incomplete"] += 1
    if (first_bad is None) != r["equity_complete"]:
        issues["complete_flag_mismatch"] += 1
    # once incomplete, always incomplete?
    if first_bad is not None and any(e[2] for e in curve[first_bad:]):
        issues["incomplete_then_complete_again"] += 1
        examples.setdefault("incomplete_then_complete_again", key)
    eq = [float(e[1]) for e in prefix]
    h = r["headline"]
    if len(eq) < 2:
        continue
    rets = [b / a - 1 for a, b in zip(eq, eq[1:])]
    n = len(rets)
    tot = eq[-1] / eq[0] - 1
    ann = (1 + tot) ** (252 / n) - 1
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    mu = sum(rets) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (n - 1))
    dd = math.sqrt(sum(min(x, 0) ** 2 for x in rets) / n)
    sharpe = mu / sd * math.sqrt(252) if sd else None
    sortino = mu / dd * math.sqrt(252) if dd else None
    for name, mine, theirs, tol in (("total", tot, h["total_return"], 1e-6), ("annual", ann, h["annualized_return"], 1e-6),
                                     ("mdd", mdd, h["max_drawdown"], 1e-6), ("sharpe", sharpe, h["sharpe"], 1e-4),
                                     ("sortino", sortino, h["sortino"], 1e-4)):
        if (mine is None) != (theirs is None) or (mine is not None and abs(mine - theirs) > tol):
            issues[f"metric_{name}"] += 1
            examples.setdefault(f"metric_{name}", (key, mine, theirs))
    if h["end"] != prefix[-1][0] or h["sessions"] != n:
        issues["headline_window"] += 1
    # trade metrics from the trade list
    pn = [D(t["pnl"]) for t in r["trade_list"]]
    w = [p for p in pn if p > 0]
    l = [p for p in pn if p <= 0]
    tm = r["trades"]
    if tm["closed_trades"] != len(pn) or tm["wins"] != len(w):
        issues["trade_counts"] += 1
    pf = float(sum(w) / -sum(l)) if l and sum(l) != 0 else None
    if (pf is None) != (tm["profit_factor"] is None) or (pf is not None and abs(pf - tm["profit_factor"]) > 1e-4):
        issues["profit_factor"] += 1
    # identity: complete run, nothing open -> end equity - capital == sum of closed-trade P&L
    if r["equity_complete"] and not r["open_positions_at_end"]:
        diff = D(curve[-1][1]) - D(r["account"]["capital"]) - sum(pn, D(0))
        if abs(diff) > D("0.000001"):
            issues["trade_sum_identity"] += 1
            examples.setdefault("trade_sum_identity", (key, str(diff)))
        else:
            issues["trade_sum_identity_ok"] += 1
    # fraction of flat sessions in the headline window (exposure context for Sharpe)
    flat = sum(1 for a, b in zip(eq, eq[1:]) if a == b)
    if r["scenario"] == "baseline":
        examples.setdefault("flat_share", {})[f"{key[0]}|{key[1]}"] = round(flat / n, 3)
print(json.dumps(issues, indent=1))
print(json.dumps(examples, indent=1, default=str)[:4000])
# turnover/cost basis mismatch for incomplete runs
for r in doc["runs"]:
    if r["scenario"] == "baseline" and not r["equity_complete"]:
        print("INCOMPLETE", r["strategy_set"], r["period"], "headline sessions", r["headline"]["sessions"],
              "trades counted", r["trades"]["closed_trades"], "trades closed after prefix end",
              sum(1 for t in r["trade_list"] if t["exit_session"] > r["headline"]["end"]),
              "turnover", r["exposure"]["annual_turnover"], "costs", r["costs"]["total"])

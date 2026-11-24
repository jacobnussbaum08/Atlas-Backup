"""Independent re-evaluation of the stored X7 V2 results (pre-registration section 2).

Written from the text of ``config/simulation/x7/v2_preregistration.md``; it imports nothing from
the evaluator or the pipeline. It reads only the stored V2 results artifact and the frozen
evaluator's stored evaluation, recomputes every input to the rule, applies the rule and compares:
outcome, reason codes, R1-R5 and K1-K7, pooled statistics, bootstrap counts and p-values, Holm
p-values, annual excess and condition means. The bootstrap uses the same CPython generator and
call sequence (section 2.1.1), so its counts must match exactly.

Usage: python config/simulation/x7_v2_verification/reevaluate_v2_results.py
"""

import gzip
import json
import math
import random
import statistics
import sys
from pathlib import Path

RESULTS = Path("config/simulation/lifecycle/x7_backtest_results_v2.json.gz")
EVALUATION = Path("config/simulation/lifecycle/x7_v2_evaluation_v1.json.gz")
REGISTERED = ("breakout", "mean_reversion", "momentum", "trend_following", "volatility")
COMBINED = ("combined_sleeves", "combined_round_robin")
YEARS = ("year_2017", "year_2018", "year_2019")
LS = (5, 10, 20)


def read(p):
    return json.loads(gzip.decompress(p.read_bytes()))


def account(run, spy):
    """Section 2.1: daily net return, daily excess, exposure at the previous close."""
    s, eq, lmv = run["series"]["sessions"], run["series"]["equity"], run["series"]["long_market_value"]
    out = {"r": [], "x": [], "e": [], "lab_session": [], "n": len(eq) - 1}
    strat, bench = 1.0, 1.0
    for t in range(1, len(eq)):
        r = eq[t] / eq[t - 1] - 1
        e = lmv[t - 1] / eq[t - 1]
        b = spy[s[t]] - 1
        out["r"].append(r)
        out["x"].append(r - e * b)
        out["e"].append(e)
        out["lab_session"].append(s[t - 1])  # labels at the close of t-1 (section 2.3)
        strat *= 1 + r
        bench *= 1 + e * b
    out["annual_excess"] = (strat - 1) - (bench - 1)
    out["net"] = eq[-1] - eq[0]
    out["trades"] = [float(t["pnl"]) for t in run["trade_list"] if t["exit_session"] <= s[-1]]
    return out


def bootstrap(arrays, L, B=10_000, seed=20261121):
    """Section 2.1.1, from its pseudo-code."""
    arrays = [a for a in arrays if len(a) > 0]
    n = sum(len(a) for a in arrays)
    if n == 0:
        return {"p_pos": 1.0, "p_neg": 1.0, "count_pos": None, "count_neg": None, "mean": None}
    m = sum(sum(a) for a in arrays) / n
    y = [[v - m for v in a] for a in arrays]
    rng = random.Random(seed)  # noqa: S311
    q = 1 / L
    star = []
    for _b in range(B):
        total = 0.0
        for ya in y:
            na = len(ya)
            i = rng.randrange(na)
            total += ya[i]
            for _k in range(2, na + 1):
                i = rng.randrange(na) if rng.random() < q else (i + 1) % na
                total += ya[i]
        star.append(total / n)
    cp = sum(1 for v in star if v >= m)
    cn = sum(1 for v in star if v <= m)
    return {"mean": m, "count_pos": cp, "count_neg": cn if m < 0 else None,
            "p_pos": (1 + cp) / (B + 1), "p_neg": (1 + cn) / (B + 1) if m < 0 else 1.0}  # fmt: skip


def holm(p):
    order = sorted(p, key=lambda k: (p[k], k))
    m, run, out = len(order), 0.0, {}
    for j, k in enumerate(order, 1):
        run = max(run, min(1.0, (m - j + 1) * p[k]))
        out[k] = run
    return out


def rule(base, adverse, regimes, padj, boots):
    """Section 2.4 from its text."""
    N = sum(len(a["trades"]) for a in base)
    net = sum(a["net"] for a in base)
    r_all = [v for a in base for v in a["r"]]
    x_all = [v for a in base for v in a["x"]]
    mr, mx = statistics.fmean(r_all), statistics.fmean(x_all)
    pnls = [p for a in base for p in a["trades"]]
    wins = sorted((p for p in pnls if p > 0), reverse=True)
    losses = sorted(p for p in pnls if p < 0)
    gw, gl = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    adequate = [a for a in base if a["n"] >= 126 and len(a["trades"]) >= 30]
    ann = [a["annual_excess"] for a in adequate]
    adv_net = sum(a["net"] for a in adverse)
    adv_mx = statistics.fmean([v for a in adverse for v in a["x"]])
    cond = {}
    for dim, lab in (("trend", "UP"), ("trend", "DOWN"), ("volatility", "HIGH"), ("volatility", "LOW")):
        v = [x for a in base for x, e, s in zip(a["x"], a["e"], a["lab_session"], strict=True)
             if e > 0 and regimes.get(s, {}).get(dim) == lab]  # fmt: skip
        cond[f"{dim}_{lab}"] = {"active": len(v), "evaluable": len(v) >= 40, "mean": statistics.fmean(v) if v else None}
    pf_ok = (gw > 0) if gl == 0 else gw / gl >= 1.2
    R = {"R1": N >= 60, "R2": net < 0 and mr < 0,
         "R3": mx < 0 and all(boots[L]["p_neg"] < 0.05 for L in LS),
         "R4": len(adequate) >= 2 and all(v < 0 for v in ann),
         "R5": net - sum(losses[:5]) < 0}  # fmt: skip
    K = {"K1": net > 0 and mr > 0, "K2": mx > 0 and padj[10] < 0.05,
         "K3": len(adequate) >= 2 and sum(v > 0 for v in ann) >= 2 and all(v > -0.02 for v in ann),
         "K4": adv_net > 0 and adv_mx > 0,
         "K5": net - sum(wins[:5]) > 0 and pf_ok,
         "K6": all(c["evaluable"] for c in cond.values()) and all(c["mean"] >= 0 for c in cond.values())}  # fmt: skip
    k7_l = padj[5] < 0.05 and padj[20] < 0.05
    k7_w = net - sum(wins[:10]) > 0
    K["K7"] = True if N >= 200 else (k7_l and k7_w) if N >= 100 else False
    if all(R.values()):
        out = "REJECT"
    elif all(K.values()):
        out = "CANDIDATE (below evidence target)" if N < 200 else "CANDIDATE"
    else:
        out = "INCONCLUSIVE"
    codes = []
    if out == "INCONCLUSIVE":
        mid = 100 <= N < 200
        if not K["K1"]:
            codes.append("NET_NOT_POSITIVE")
        if not K["K2"] or (mid and not k7_l):
            codes.append("NOT_SIGNIFICANT")
        if N < 100 or len(adequate) < 2:
            codes.append("INSUFFICIENT_SAMPLE")
        if len(adequate) >= 2 and not K["K3"]:
            codes.append("MIXED_YEARS")
        if not K["K4"]:
            codes.append("COST_FRAGILE")
        if not K["K5"] or (mid and not k7_w):
            codes.append("CONCENTRATED")
        if not all(c["evaluable"] for c in cond.values()):
            codes.append("REGIME_UNEVALUABLE")
        if any(c["evaluable"] and c["mean"] is not None and c["mean"] < 0 for c in cond.values()):
            codes.append("REGIME_NEGATIVE")
        if R["R2"]:
            codes.append("NEGATIVE_BUT_INCONSISTENT")
    return {"outcome": out, "codes": codes, "R": R, "K": K, "N": N, "net": net, "mean_r": mr, "mean_x": mx,
            "annual": [a["annual_excess"] for a in base], "adequate": len(adequate), "cond": cond,
            "adv_net": adv_net, "adv_mean_x": adv_mx}  # fmt: skip


def close(a, b, tol=1e-12):
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def main():
    res, ev = read(RESULTS), read(EVALUATION)
    spy = {k: float(v) for k, v in res["spy_factors"].items()}
    runs = {(r["strategy_set"], r["period"], r["scenario"]): r for r in res["runs"]}
    names = REGISTERED + COMBINED
    base = {s: [account(runs[(s, y, "baseline")], spy) for y in YEARS] for s in names}
    adv = {s: [account(runs[(s, y, "all_adverse")], spy) for y in YEARS] for s in names}
    boots = {s: {L: bootstrap([a["x"] for a in base[s]], L) for L in LS} for s in names}
    padj = {L: holm({s: boots[s][L]["p_pos"] for s in REGISTERED}) for L in LS}
    report, diffs = {}, []
    for s in names:
        p = {L: padj[L][s] for L in LS} if s in REGISTERED else {L: boots[s][L]["p_pos"] for L in LS}
        mine = rule(base[s], adv[s], res["regimes"], p, boots[s])
        theirs = ev["strategies"][s]
        pb = theirs["pooled_baseline"]
        checks = {
            "R": mine["R"] == theirs["R"], "K": mine["K"] == theirs["K"],
            "N": mine["N"] == pb["N"], "net": close(mine["net"], pb["net_pnl"], 1e-9),
            "mean_r": close(mine["mean_r"], pb["mean_daily_return"]), "mean_x": close(mine["mean_x"], pb["mean_daily_excess"]),
            "annual": all(close(a, b) for a, b in zip(mine["annual"], theirs["annual_excess"], strict=True)),
            "adequate": mine["adequate"] == theirs["adequate_years"],
            "adverse": close(mine["adv_net"], theirs["pooled_all_adverse"]["net_pnl"], 1e-9)
                       and close(mine["adv_mean_x"], theirs["pooled_all_adverse"]["mean_daily_excess"]),
            "conditions": all(mine["cond"][k]["active"] == v["active_sessions"] and close(mine["cond"][k]["mean"], v["mean_excess"])
                              for k, v in theirs["conditions"].items()),
            "bootstrap_counts": all(boots[s][L]["count_pos"] == theirs["bootstrap"][str(L)]["count_pos"]
                                    and boots[s][L]["count_neg"] == theirs["bootstrap"][str(L)]["count_neg"]
                                    and boots[s][L]["p_pos"] == theirs["bootstrap"][str(L)]["p_pos"]
                                    and boots[s][L]["p_neg"] == theirs["bootstrap"][str(L)]["p_neg"] for L in LS),
            "holm": all(p[L] == theirs["holm_p_pos"][str(L)] for L in LS),
        }  # fmt: skip
        if s in REGISTERED:
            checks["outcome"] = mine["outcome"] == theirs["outcome"]
            checks["codes"] = mine["codes"] == theirs["reason_codes"]
        else:
            checks["outcome"] = theirs["outcome"] == "NOT_CLASSIFIED"
        diffs += [f"{s}:{k}" for k, ok in checks.items() if not ok]
        report[s] = {"outcome_independent": mine["outcome"] if s in REGISTERED else "NOT_CLASSIFIED",
                     "outcome_evaluator": theirs["outcome"], "codes": mine["codes"] if s in REGISTERED else [],
                     "checks": checks}  # fmt: skip
    print(json.dumps({"ok": not diffs, "differences": diffs, "strategies": report}, indent=1))
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())

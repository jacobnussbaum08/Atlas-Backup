# ruff: noqa: E501  (criterion tables read as one row each)
"""X7 V2 (D-257): the frozen decision rule, as a separate deterministic component.

Implements section 2 of ``config/simulation/x7/v2_preregistration.md`` (frozen 2026-11-21) and
nothing else. It reads only the stored V2 results artifact; it never runs a simulation and
never reads a database. Every quantity is recomputed here from the stored daily equity, long
market value, SPY factors, regime labels and trade lists, not taken from the pipeline's own
summaries.

Before evaluating it checks the freeze: the specification's and the pre-registration's SHA-256
must equal ``v2_freeze.json``, and so must this file's own SHA-256 (recorded there before the
first V2 run).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

FREEZE = Path("config/simulation/x7/v2_freeze.json")
SPEC = "docs/phase-reports/x7-v2-correction-spec.md"
PREREG = "config/simulation/x7/v2_preregistration.md"
SELF = "backend/app/research/x7v2_evaluator.py"
STRATEGIES = ("breakout", "mean_reversion", "momentum", "trend_following", "volatility")
UNCLASSIFIED = ("combined_sleeves", "combined_round_robin")
YEARS = ("year_2017", "year_2018", "year_2019")
SEED = 20261121
B = 10_000
LENGTHS = (5, 10, 20)
PRIMARY_L = 10
ALPHA = 0.05
CONDITIONS = (("trend", "UP"), ("trend", "DOWN"), ("volatility", "HIGH"), ("volatility", "LOW"))
MIN_ACTIVE = 40
ADEQUATE_SESSIONS, ADEQUATE_TRADES = 126, 30
ANNUAL_FLOOR = -0.02
PF_MIN = 1.2
N_REJECT, N_MIN, N_TARGET = 60, 100, 200


# ---------------------------------------------------------------------- the bootstrap (2.1.1)
def quantile7(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (Hyndman-Fan type 7; NumPy's default)."""
    xs = sorted(values)
    h = (len(xs) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def stationary_bootstrap(
    arrays: list[list[float]], length: int, draws: int = B, seed: int = SEED
) -> dict[str, Any]:
    """Section 2.1.1, exactly: pooled mean, centred series, circular within each account,
    a fresh ``random.Random(seed)`` per call, accounts in the order given (empty ones skipped)."""
    arrays = [a for a in arrays if a]
    n = sum(len(a) for a in arrays)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "p_pos": 1.0,
            "p_neg": 1.0,
            "ci95": None,
            "count_pos": None,
            "count_neg": None,
        }
    m_hat = sum(sum(a) for a in arrays) / n
    centred = [[v - m_hat for v in a] for a in arrays]
    rng = random.Random(seed)  # noqa: S311  (a reproducible statistical resampler, not cryptography)
    q = 1 / length
    means: list[float] = []
    for _ in range(draws):
        total = 0.0
        for y in centred:
            n_a = len(y)
            i = rng.randrange(n_a)
            total += y[i]
            for _k in range(2, n_a + 1):
                if rng.random() < q:  # noqa: SIM108  (written as in spec 2.1.1)
                    i = rng.randrange(n_a)
                else:
                    i = (i + 1) % n_a
                total += y[i]
        means.append(total / n)
    c_pos = sum(1 for v in means if v >= m_hat)
    c_neg = sum(1 for v in means if v <= m_hat)
    return {
        "n": n, "lengths": [len(a) for a in arrays], "L": length, "draws": draws, "seed": seed, "mean": m_hat,
        "count_pos": c_pos, "count_neg": c_neg if m_hat < 0 else None,
        "p_pos": (1 + c_pos) / (draws + 1), "p_neg": (1 + c_neg) / (draws + 1) if m_hat < 0 else 1.0,
        "ci95": [m_hat - quantile7(means, 0.975), m_hat - quantile7(means, 0.025)],
        "first_means": means[:5],
    }  # fmt: skip


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values over a fixed family (ties ordered by name)."""
    order = sorted(pvalues, key=lambda k: (pvalues[k], k))
    m = len(order)
    out: dict[str, float] = {}
    running = 0.0
    for j, k in enumerate(order, start=1):
        running = max(running, min(1.0, (m - j + 1) * pvalues[k]))
        out[k] = running
    return out


# ---------------------------------------------------------------------- one account's series
def account_series(run: dict[str, Any], spy: dict[str, float]) -> dict[str, Any]:
    """Complete-basis daily net returns, exposures and excess from the stored series."""
    eq = run["series"]["equity"]
    lmv = run["series"]["long_market_value"]
    sessions = run["series"]["sessions"]
    r, x, prev_exposure, prev_sessions = [], [], [], []
    for t in range(1, len(eq)):
        rt = eq[t] / eq[t - 1] - 1
        e_prev = lmv[t - 1] / eq[t - 1]
        rs = spy[sessions[t]] - 1
        r.append(rt)
        x.append(rt - e_prev * rs)
        prev_exposure.append(e_prev)
        prev_sessions.append(sessions[t - 1])
    end = sessions[-1]
    trades = [t for t in run["trade_list"] if t["exit_session"] <= end]
    spy_path = 1.0
    strat_path = 1.0
    for rt, e_prev, s in zip(r, prev_exposure, sessions[1:], strict=True):
        strat_path *= 1 + rt
        spy_path *= 1 + e_prev * (spy[s] - 1)
    return {"r": r, "x": x, "e_prev": prev_exposure, "prev_sessions": prev_sessions, "trades": trades,
            "net_pnl": eq[-1] - eq[0], "annual_excess": (strat_path - 1) - (spy_path - 1), "n": len(r)}  # fmt: skip


def _pnl(t: dict[str, Any]) -> float:
    return float(t["pnl"])


def pooled(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    r = [v for a in accounts for v in a["r"]]
    x = [v for a in accounts for v in a["x"]]
    trades = [t for a in accounts for t in a["trades"]]
    pnls = [_pnl(t) for t in trades]
    wins = sorted((p for p in pnls if p > 0), reverse=True)
    losses = sorted(p for p in pnls if p < 0)
    gross_win, gross_loss = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p <= 0)
    net = sum(a["net_pnl"] for a in accounts)
    return {
        "N": len(trades), "net_pnl": net,
        "mean_daily_return": statistics.fmean(r) if r else None,
        "mean_daily_excess": statistics.fmean(x) if x else None,
        # No losing trade: the factor is +infinity (it clears any bar) when there is a win;
        # with no trade or no win it is undefined (it clears none).
        "profit_factor": None if gross_loss == 0 else gross_win / gross_loss,
        "profit_factor_meets_bar": (gross_win > 0) if gross_loss == 0 else gross_win / gross_loss >= PF_MIN,
        "net_without_5_best": net - sum(wins[:5]), "net_without_10_best": net - sum(wins[:10]),
        "net_without_5_worst": net - sum(losses[:5]),
    }  # fmt: skip


def conditions(
    accounts: list[dict[str, Any]], regimes: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Active sessions and mean daily excess per market condition (labels of the previous close)."""
    out: dict[str, Any] = {}
    for dim, label in CONDITIONS:
        vals = [x for a in accounts for x, e, s in zip(a["x"], a["e_prev"], a["prev_sessions"], strict=True)
                if e > 0 and regimes.get(s, {}).get(dim) == label]  # fmt: skip
        out[f"{dim}_{label}"] = {"active_sessions": len(vals), "evaluable": len(vals) >= MIN_ACTIVE,
                                 "mean_excess": statistics.fmean(vals) if vals else None}  # fmt: skip
    return out


# ---------------------------------------------------------------------- the rule (2.4)
def evaluate_strategy(base: list[dict[str, Any]], adverse: list[dict[str, Any]], regimes: dict[str, dict[str, str]],
                      p_adj: dict[int, float], boots: dict[int, dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    pb, pa = pooled(base), pooled(adverse)
    adequate = [
        a for a in base if a["n"] >= ADEQUATE_SESSIONS and len(a["trades"]) >= ADEQUATE_TRADES
    ]
    annual = [a["annual_excess"] for a in adequate]
    cond = conditions(base, regimes)
    mx, mr, net, n = pb["mean_daily_excess"], pb["mean_daily_return"], pb["net_pnl"], pb["N"]
    R = {
        "R1": n >= N_REJECT,
        "R2": net < 0 and mr is not None and mr < 0,
        "R3": mx is not None and mx < 0 and all(boots[L]["p_neg"] < ALPHA for L in LENGTHS),
        "R4": len(adequate) >= 2 and all(v < 0 for v in annual),
        "R5": pb["net_without_5_worst"] < 0,
    }
    K = {
        "K1": net > 0 and mr is not None and mr > 0,
        "K2": mx is not None and mx > 0 and p_adj[PRIMARY_L] < ALPHA,
        "K3": len(adequate) >= 2
        and sum(1 for v in annual if v > 0) >= 2
        and all(v > ANNUAL_FLOOR for v in annual),
        "K4": pa["net_pnl"] > 0
        and pa["mean_daily_excess"] is not None
        and pa["mean_daily_excess"] > 0,
        "K5": pb["net_without_5_best"] > 0 and pb["profit_factor_meets_bar"],
        "K6": all(c["evaluable"] for c in cond.values())
        and all(c["mean_excess"] >= 0 for c in cond.values()),
    }
    if n >= N_TARGET:
        k7, k7_parts = True, {}
    elif n >= N_MIN:
        k7_parts = {"bootstrap_L5_L20": p_adj[5] < ALPHA and p_adj[20] < ALPHA,
                    "without_10_best": pb["net_without_10_best"] > 0}  # fmt: skip
        k7 = all(k7_parts.values())
    else:
        k7, k7_parts = False, {}
    K["K7"] = k7
    if all(R.values()):
        outcome = "REJECT"
    elif all(K.values()):
        outcome = "CANDIDATE (below evidence target)" if n < N_TARGET else "CANDIDATE"
    else:
        outcome = "INCONCLUSIVE"
    codes: list[str] = []
    if outcome == "INCONCLUSIVE":
        if not K["K1"]:
            codes.append("NET_NOT_POSITIVE")
        if not K["K2"] or (N_MIN <= n < N_TARGET and not k7_parts.get("bootstrap_L5_L20", True)):
            codes.append("NOT_SIGNIFICANT")
        if n < N_MIN or len(adequate) < 2:
            codes.append("INSUFFICIENT_SAMPLE")
        if len(adequate) >= 2 and not K["K3"]:
            codes.append("MIXED_YEARS")
        if not K["K4"]:
            codes.append("COST_FRAGILE")
        if not K["K5"] or (N_MIN <= n < N_TARGET and not k7_parts.get("without_10_best", True)):
            codes.append("CONCENTRATED")
        if not all(c["evaluable"] for c in cond.values()):
            codes.append("REGIME_UNEVALUABLE")
        if any(
            c["evaluable"] and c["mean_excess"] is not None and c["mean_excess"] < 0
            for c in cond.values()
        ):
            codes.append("REGIME_NEGATIVE")
        if R["R2"]:
            codes.append("NEGATIVE_BUT_INCONSISTENT")
    return {"outcome": outcome, "reason_codes": codes, "R": R, "K": K, "K7_parts": k7_parts,
            "pooled_baseline": pb, "pooled_all_adverse": pa, "adequate_years": len(adequate),
            "annual_excess": [a["annual_excess"] for a in base], "conditions": cond,
            "bootstrap": {str(L): boots[L] for L in LENGTHS}, "holm_p_pos": {str(L): p_adj[L] for L in LENGTHS}}  # fmt: skip


def evaluate(results: dict[str, Any]) -> dict[str, Any]:
    """The rule on a V2 results document (pure)."""
    if results.get("methodology_version") != 2:
        raise ValueError("not a V2 results document")
    spy = {s: float(g) for s, g in results["spy_factors"].items()}
    regimes = results["regimes"]
    runs = {(r["strategy_set"], r["period"], r["scenario"]): r for r in results["runs"]}
    names = (*STRATEGIES, *UNCLASSIFIED)
    acc = {s: [account_series(runs[(s, y, "baseline")], spy) for y in YEARS] for s in names}
    adv = {s: [account_series(runs[(s, y, "all_adverse")], spy) for y in YEARS] for s in names}
    boots = {
        s: {L: stationary_bootstrap([a["x"] for a in acc[s]], L, B) for L in LENGTHS} for s in names
    }
    p_adj = {L: holm({s: boots[s][L]["p_pos"] for s in STRATEGIES}) for L in LENGTHS}
    out: dict[str, Any] = {}
    for s in STRATEGIES:
        out[s] = evaluate_strategy(
            acc[s], adv[s], regimes, {L: p_adj[L][s] for L in LENGTHS}, boots[s]
        )
    for s in UNCLASSIFIED:
        res = evaluate_strategy(
            acc[s], adv[s], regimes, {L: boots[s][L]["p_pos"] for L in LENGTHS}, boots[s]
        )
        res["outcome"] = "NOT_CLASSIFIED"
        res["reason_codes"] = []
        res["note"] = (
            "a combined allocation is not a registered strategy version; Holm is not applied"
        )
        out[s] = res
    return {"kind": "x7v2_evaluation", "decision": "D-257", "rule": PREREG, "strategies": out}


# ---------------------------------------------------------------------- the freeze
def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_freeze() -> dict[str, Any]:
    """The spec, the pre-registration and this file must match the freeze manifest."""
    man = json.loads(FREEZE.read_bytes())
    found = {p: _sha(p) for p in man["files"]}
    bad = {p: (h, found[p]) for p, h in man["files"].items() if found[p] != h}
    ev = man.get("evaluator") or {}
    if not ev.get("sha256") or ev.get("path") != SELF:
        bad[SELF] = ("not recorded", _sha(SELF))
    elif _sha(SELF) != ev["sha256"]:
        bad[SELF] = (ev["sha256"], _sha(SELF))
    if bad:
        raise PermissionError(f"the V2 freeze is not intact: {bad}")
    return {"files": found, "evaluator": _sha(SELF)}


def cli_evaluate() -> dict[str, Any]:
    from app.research import x7v2_pipeline as p2

    freeze = check_freeze()
    results = p2.load_results()
    doc = evaluate(results)
    doc["freeze"] = freeze
    doc["results_content_sha256"] = results["content_sha256"]
    return {"outcomes": {s: (v["outcome"], v["reason_codes"]) for s, v in doc["strategies"].items()},
            "written": p2.write_evaluation(doc)}  # fmt: skip

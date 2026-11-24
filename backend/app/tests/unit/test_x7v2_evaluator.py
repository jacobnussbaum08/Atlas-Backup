# ruff: noqa: E501, PT018  (fixture rows on one line; paired assertions read as one fact)
"""X7 V2 (D-257): the frozen decision rule (section 2), on FIXTURE results with known answers.

Every series and trade below is constructed for the test; nothing is a backtest result.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

from app.research import x5_accounting as acc
from app.research import x7v2_classification as cls
from app.research import x7v2_evaluator as ev


# ---------------------------------------------------------------------- the bootstrap (2.1.1)
def reference_bootstrap(
    arrays: list[list[float]], length: int, draws: int, seed: int
) -> dict[str, Any]:
    """A second, separately written implementation: index lists first, then the means."""
    arrays = [a for a in arrays if a]
    n = sum(map(len, arrays))
    m = math.fsum(v for a in arrays for v in a) / n
    rng = random.Random(seed)  # noqa: S311  (reproducible resampling, not cryptography)
    means, all_indices = [], []
    for _ in range(draws):
        idx_lists = []
        for a in arrays:
            seq = [rng.randrange(len(a))]
            while len(seq) < len(a):
                seq.append(
                    rng.randrange(len(a)) if rng.random() < 1 / length else (seq[-1] + 1) % len(a)
                )
            idx_lists.append(seq)
        all_indices.append(idx_lists)
        total = 0.0
        for a, seq in zip(arrays, idx_lists, strict=True):
            for i in seq:
                total += a[i] - m
        means.append(total / n)
    return {"m": m, "means": means, "indices": all_indices}


ARR = [[0.01, -0.02, 0.03, 0.0, 0.01], [0.02, -0.01, 0.0], [0.005, 0.0, -0.005, 0.01]]


def test_bootstrap_matches_an_independent_implementation_exactly():
    got = ev.stationary_bootstrap(ARR, 2, draws=20, seed=20261121)
    ref = reference_bootstrap(ARR, 2, 20, 20261121)
    assert got["mean"] == pytest.approx(ref["m"], abs=1e-18)
    assert got["first_means"] == pytest.approx(ref["means"][:5], abs=1e-15)
    assert got["count_pos"] == sum(1 for v in ref["means"] if v >= got["mean"])
    assert got["p_pos"] == (1 + got["count_pos"]) / 21
    # indices stay inside their own account and each account keeps its length
    for idx_lists in ref["indices"]:
        assert [len(s) for s in idx_lists] == [5, 3, 4]
        assert all(0 <= i < len(a) for a, s in zip(ARR, idx_lists, strict=True) for i in s)


def test_bootstrap_negative_side_and_ties():
    neg = ev.stationary_bootstrap([[-0.01, -0.02, -0.01, 0.0]], 2, draws=50)
    assert neg["mean"] < 0 and neg["p_neg"] == (1 + neg["count_neg"]) / 51
    flat = ev.stationary_bootstrap([[0.0, 0.0, 0.0]], 2, draws=10)
    assert (
        flat["p_pos"] == 1.0 and flat["p_neg"] == 1.0
    )  # m = 0: every mean* = 0 ties with it; no negative evidence


def test_bootstrap_is_deterministic_and_seeded():
    a = ev.stationary_bootstrap(ARR, 5, draws=30)
    b = ev.stationary_bootstrap(ARR, 5, draws=30)
    c = ev.stationary_bootstrap(ARR, 5, draws=30, seed=1)
    assert a == b and a["first_means"] != c["first_means"]


def test_bootstrap_interval_is_the_basic_interval_with_type_7_quantiles():
    got = ev.stationary_bootstrap(ARR, 2, draws=20)
    ref = reference_bootstrap(ARR, 2, 20, 20261121)
    xs = sorted(ref["means"])

    def q7(p: float) -> float:
        h = (len(xs) - 1) * p
        lo = math.floor(h)
        return float(xs[lo] + (h - lo) * (xs[min(lo + 1, len(xs) - 1)] - xs[lo]))

    assert got["ci95"] == pytest.approx([ref["m"] - q7(0.975), ref["m"] - q7(0.025)], abs=1e-15)


def test_holm_matches_a_hand_calculation():
    p = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.2, "e": 1.0}
    # sorted a .01 (x5 = .05), c .03 (x4 = .12), b .04 (x3 = .12), d .2 (x2 = .4), e 1 (x1 = 1)
    assert ev.holm(p) == pytest.approx({"a": 0.05, "c": 0.12, "b": 0.12, "d": 0.4, "e": 1.0})


# ---------------------------------------------------------------------- fixture results
SESS = {
    y: acc._sessions(f"{y}-01-03" if y == "2017" else f"{y}-01-02", f"{y}-12-29")
    for y in ("2017", "2018", "2019")
}
START = {"2017": "2016-12-30", "2018": "2017-12-29", "2019": "2018-12-31"}
SPY = 1.0004


def make_run(name: str, year: str, scenario: str, excess: float, trades: int, trade_pnl: float = 10.0,
             exposure: float = 0.5, sessions: int = 200) -> dict[str, Any]:  # fmt: skip
    ss = [START[year], *SESS[year][:sessions]]
    eq, lmv = [100000.0], [100000.0 * exposure]
    for _ in ss[1:]:
        r = exposure * (SPY - 1) + excess
        eq.append(eq[-1] * (1 + r))
        lmv.append(eq[-1] * exposure)
    tl = [{"slot": f"STOCK|x|T{i}", "strategy_id": name, "exit_session": ss[1 + (i % sessions)],
           "pnl": str(trade_pnl if i % 3 else trade_pnl * 0.5), "entry_price": "100", "entry_quantity": "10", "initial_stop": "95"}
          for i in range(trades)]  # fmt: skip
    return {"strategy_set": name, "period": f"year_{year}", "scenario": scenario,
            "series": {"sessions": ss, "equity": eq, "long_market_value": lmv}, "trade_list": tl}  # fmt: skip


def make_doc(
    spec: dict[str, dict[str, Any]], regimes: dict[str, dict[str, str]] | None = None
) -> dict[str, Any]:
    runs = []
    for name in (*ev.STRATEGIES, *ev.UNCLASSIFIED):
        s = spec.get(name, {"excess": {"2017": 0.0, "2018": 0.0, "2019": 0.0}, "trades": 0})
        for y in ("2017", "2018", "2019"):
            for sc in ("baseline", "all_adverse"):
                ex_ = s["excess"][y] + (
                    s.get("adverse_shift", -0.0001) if sc == "all_adverse" else 0.0
                )
                runs.append(make_run(name, y, sc, ex_, s["trades"] // 3 + (1 if y == "2017" and s["trades"] % 3 else 0),
                                     trade_pnl=s.get("pnl", 10.0), sessions=s.get("sessions", 200)))  # fmt: skip
    spy = {d: SPY for y in SESS for d in SESS[y]}
    if regimes is None:
        regimes = {}
        for y in SESS:
            for i, d in enumerate([START[y], *SESS[y]]):
                regimes[d] = {
                    "trend": "UP" if i % 2 else "DOWN",
                    "volatility": "HIGH" if (i // 2) % 2 else "LOW",
                }
    return {"methodology_version": 2, "spy_factors": spy, "regimes": regimes, "runs": runs}


GOOD = {"excess": {"2017": 0.002, "2018": 0.002, "2019": 0.002}, "trades": 240, "pnl": 10.0}
BAD = {"excess": {"2017": -0.002, "2018": -0.002, "2019": -0.002}, "trades": 240, "pnl": -10.0}


@pytest.fixture(autouse=True)
def _fast_bootstrap(monkeypatch):
    monkeypatch.setattr(ev, "B", 200)


def outcome(doc: dict[str, Any], name: str) -> dict[str, Any]:
    res: dict[str, Any] = ev.evaluate(doc)["strategies"][name]
    return res


def test_a_consistent_strong_positive_strategy_is_a_candidate():
    res = outcome(make_doc({"mean_reversion": GOOD}), "mean_reversion")
    assert res["outcome"] == "CANDIDATE" and all(res["K"].values()) and res["reason_codes"] == []


def test_a_consistent_strong_negative_strategy_is_rejected_only_with_all_of_r1_to_r5():
    res = outcome(make_doc({"breakout": BAD}), "breakout")
    assert res["outcome"] == "REJECT" and all(res["R"].values())


def test_negative_with_one_positive_year_is_inconclusive_not_rejected():
    mixed = {**BAD, "excess": {"2017": -0.002, "2018": -0.002, "2019": 0.0015}}
    res = outcome(make_doc({"breakout": mixed}), "breakout")
    assert res["outcome"] == "INCONCLUSIVE" and not res["R"]["R4"]
    assert "NEGATIVE_BUT_INCONSISTENT" in res["reason_codes"] or not res["R"]["R2"]


def test_too_few_trades_never_rejects_and_never_advances():
    res = outcome(make_doc({"breakout": {**BAD, "trades": 45}}), "breakout")
    assert (
        res["outcome"] == "INCONCLUSIVE"
        and not res["R"]["R1"]
        and "INSUFFICIENT_SAMPLE" in res["reason_codes"]
    )
    res2 = outcome(make_doc({"momentum": {**GOOD, "trades": 99}}), "momentum")
    assert res2["outcome"] == "INCONCLUSIVE" and "INSUFFICIENT_SAMPLE" in res2["reason_codes"]


def test_below_the_evidence_target_a_candidate_is_labelled():
    res = outcome(make_doc({"momentum": {**GOOD, "trades": 150}}), "momentum")
    assert res["outcome"] == "CANDIDATE (below evidence target)" and all(res["K7_parts"].values())


def test_doubled_costs_failing_blocks_a_candidate():
    res = outcome(make_doc({"momentum": {**GOOD, "adverse_shift": -0.005}}), "momentum")
    assert (
        res["outcome"] == "INCONCLUSIVE"
        and "COST_FRAGILE" in res["reason_codes"]
        and not res["K"]["K4"]
    )


def test_unknown_regimes_make_the_conditions_unevaluable():
    res = outcome(make_doc({"momentum": GOOD}, regimes={}), "momentum")
    assert res["outcome"] == "INCONCLUSIVE" and res["reason_codes"] == ["REGIME_UNEVALUABLE"]


def test_combined_allocations_are_never_classified():
    res = outcome(make_doc({"combined_sleeves": GOOD}), "combined_sleeves")
    assert res["outcome"] == "NOT_CLASSIFIED" and res["reason_codes"] == []


def test_short_years_are_not_adequate():
    res = outcome(make_doc({"momentum": {**GOOD, "sessions": 125}}), "momentum")
    assert res["adequate_years"] == 0 and "INSUFFICIENT_SAMPLE" in res["reason_codes"]


def test_random_fixtures_are_consistent():
    rng = random.Random(7)  # noqa: S311  (fixture generator)
    for _ in range(12):
        spec = {}
        for name in ev.STRATEGIES:
            spec[name] = {"excess": {y: rng.uniform(-0.003, 0.003) for y in ("2017", "2018", "2019")},
                          "trades": rng.choice([30, 80, 150, 240]), "pnl": rng.uniform(-20, 20),
                          "adverse_shift": rng.uniform(-0.002, 0.0)}  # fmt: skip
        out = ev.evaluate(make_doc(spec))["strategies"]
        for name in ev.STRATEGIES:
            r = out[name]
            assert not (all(r["R"].values()) and all(r["K"].values()))
            assert r["outcome"] in (
                "REJECT",
                "INCONCLUSIVE",
                "CANDIDATE",
                "CANDIDATE (below evidence target)",
            )
            if r["outcome"] == "INCONCLUSIVE":
                assert r["reason_codes"]
            if r["outcome"] == "REJECT":
                assert all(r["R"].values())


def test_r_multiples_are_not_used_by_any_criterion():
    a = make_doc({"mean_reversion": GOOD})
    b = make_doc({"mean_reversion": GOOD})
    for run in b["runs"]:
        for t in run["trade_list"]:
            t["initial_stop"] = "1"  # moves every stop
    assert outcome(a, "mean_reversion")["outcome"] == outcome(b, "mean_reversion")["outcome"]


# ---------------------------------------------------------------------- the freeze
def test_the_freeze_check_refuses_tampering(tmp_path, monkeypatch):
    spec, pre, me = tmp_path / "spec.md", tmp_path / "pre.md", tmp_path / "eval.py"
    for f, body in ((spec, b"spec"), (pre, b"rule"), (me, b"code")):
        f.write_bytes(body)
    man = {"files": {str(spec): hashlib.sha256(b"spec").hexdigest(), str(pre): hashlib.sha256(b"rule").hexdigest()},
           "evaluator": {"path": str(me), "sha256": hashlib.sha256(b"code").hexdigest()}}  # fmt: skip
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps(man))
    monkeypatch.setattr(ev, "FREEZE", freeze)
    monkeypatch.setattr(ev, "SELF", str(me))
    assert ev.check_freeze()["evaluator"] == hashlib.sha256(b"code").hexdigest()
    pre.write_bytes(b"rule changed")
    with pytest.raises(PermissionError, match="not intact"):
        ev.check_freeze()
    pre.write_bytes(b"rule")
    man["evaluator"] = {"path": "", "sha256": ""}
    freeze.write_text(json.dumps(man))
    with pytest.raises(PermissionError, match="not intact"):
        ev.check_freeze()


def test_the_real_freeze_files_match_the_manifest():
    man = json.loads(Path("config/simulation/x7/v2_freeze.json").read_bytes())
    for path, h in man["files"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == h


# ---------------------------------------------------------------------- C2 classification rule
@pytest.mark.parametrize(("name", "lev", "inv", "vix"), [
    ("ProShares UltraPro Short S&P500", 3, True, False), ("ProShares Ultra QQQ", 2, False, False),
    ("ProShares UltraShort 20+ Year Treasury", 2, True, False), ("Direxion Daily Financial Bear 3X Shares", 3, True, False),
    ("Direxion Daily S&P 500 Bull 3X Shares", 3, False, False), ("ProShares Short S&P500", 1, True, False),
    ("ProShares Short VIX Short Term Futures ETF", 1, True, True), ("ProShares Trust Ultra VIX Short Term Futures ETF", 2, False, True),
    ("Vanguard Short-Term Bond ETF", 1, False, False), ("PIMCO Enhanced Short Maturity Active Exchange-Traded Fund", 1, False, False),
    ("iShares Short Treasury Bond ETF", 1, False, False), ("PowerShares S&P 500 Low Volatility Portfolio ETF", 1, False, False),
])  # fmt: skip
def test_the_classification_rule(name, lev, inv, vix):
    assert cls.classify_name(name) == {"leverage": lev, "inverse": inv, "vix_futures": vix}


# REDACTED_FOR_GITHUB: test of withheld research data removed


def test_profit_factor_with_no_losses_clears_the_bar_and_a_low_factor_does_not():
    p = ev.pooled(
        [{"r": [0.001], "x": [0.001], "trades": [{"pnl": "5"}, {"pnl": "7"}], "net_pnl": 12.0}]
    )
    assert p["profit_factor"] is None and p["profit_factor_meets_bar"] is True
    q = ev.pooled(
        [{"r": [0.001], "x": [0.001], "trades": [{"pnl": "11"}, {"pnl": "-10"}], "net_pnl": 1.0}]
    )
    assert q["profit_factor"] == pytest.approx(1.1) and q["profit_factor_meets_bar"] is False
    z = ev.pooled([{"r": [], "x": [], "trades": [], "net_pnl": 0.0}])
    assert z["profit_factor_meets_bar"] is False


# REDACTED_FOR_GITHUB: test of withheld research data removed

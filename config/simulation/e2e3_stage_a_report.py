"""E2/E3 stage A: artifacts and descriptive statistics (read only; decides nothing).

The pre-registered verdicts come only from ``e2e3_evaluate.py``. This script records, for
every one of the 45 stage-A configurations, its run, configuration hash, execution model,
data source, dataset and result hashes, and reports the descriptive statistics asked for in
the review: net and gross profit factors, expectancy in R with a 95% confidence interval
(week-clustered), maximum drawdown, Sharpe and Sortino (the engine's stored portfolio
metrics), returns by window, year and SPY regime, dividends and costs. The "combined"
figures aggregate the separate standalone accounts of the three E2 families (their daily
equity summed); no combined account was run, because none was pre-registered.

Writes runs/e2e3_stage_a_artifacts.json and runs/e2e3_stage_a_stats.json.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any

import numpy as np
from analyze import MANIFEST, reg_zone, spy_regimes
from diagnose import clustered, run_id
from scipy import stats as sps
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    BacktestEquitySnapshotRecord,
    BacktestMetricRecord,
    BacktestProposalRecord,
    BacktestRunRecord,
    BacktestTradeRecord,
)
from app.database.sessions import build_engine, build_session_factory
from app.learning import regimes as reg

ART = MANIFEST.parent / "e2e3_stage_a_artifacts.json"
OUT = MANIFEST.parent / "e2e3_stage_a_stats.json"
WINDOWS = ("a2020h2", "a2021h1", "a2021h2")
TESTS = [("e2", "momentum"), ("e2", "breakout"), ("e2", "trend_following"), ("e3", "momentum")]
VARIANTS = ("primary", "old-conventions", "spread2x")
PORTFOLIO = ("net_pnl", "gross_pnl", "total_return", "max_drawdown", "max_drawdown_pct",
             "sharpe_ratio", "sortino_ratio", "gross_sharpe_ratio", "profit_factor",
             "trades_closed", "trades_open_at_end", "win_rate", "total_costs", "commissions",
             "fees", "borrow_costs", "volatility_annualized")  # fmt: skip


def keys() -> list[str]:
    out = []
    for w in WINDOWS:
        for exp, fam in TESTS:
            out += [f"{w}--{exp}-{v}--{fam}" for v in VARIANTS]
        out += [
            f"{w}--control-primary--{fam}" for fam in ("momentum", "breakout", "trend_following")
        ]
    return out


def ci(values: list[float], clusters: list[Any]) -> dict[str, Any]:
    c = clustered(values, clusters)
    if c.get("n", 0) < 2 or not c.get("t_clustered"):
        return c
    se = abs(c["mean"] / c["t_clustered"])
    df = max(int(c.get("clusters", 2)) - 1, 1)
    q = float(sps.t.ppf(0.975, df))
    return {**c, "se": se, "ci95": [c["mean"] - q * se, c["mean"] + q * se]}


def trade_rows(session: Any, rid: Any, regimes: dict) -> list[dict[str, Any]]:
    props = {
        p.seq: p
        for p in session.scalars(
            select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
        )
    }
    out = []
    for t in session.scalars(
        select(BacktestTradeRecord).where(
            BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
        )
    ):
        p = props[t.proposal_seq]
        risk = abs(float(t.entry_price) - float(p.invalidation_level)) * float(t.entry_quantity)
        g = reg.known_before(regimes, t.opened_at.astimezone(reg_zone()).date())
        iso = p.session.isocalendar()
        out.append(
            {
                "R": float(t.net_pnl) / risk if risk else 0.0,
                "net": float(t.net_pnl),
                "gross": float(t.realized_gross),
                "costs": float(t.costs),
                "dividends": float(t.details.get("dividends", 0) or 0),
                "week": (iso[0], iso[1]),
                "direction": t.direction,
                "trend": g.trend,
                "volatility": g.volatility,
                "exit": t.exit_reason,
            }
        )
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    gw = sum(r["gross"] for r in rows if r["gross"] > 0)
    gl = -sum(r["gross"] for r in rows if r["gross"] < 0)
    nw = sum(r["net"] for r in rows if r["net"] > 0)
    nl = -sum(r["net"] for r in rows if r["net"] < 0)
    r = ci([x["R"] for x in rows], [x["week"] for x in rows])
    return {
        "n": len(rows),
        "net": round(sum(x["net"] for x in rows), 2),
        "gross": round(sum(x["gross"] for x in rows), 2),
        "costs": round(sum(x["costs"] for x in rows), 2),
        "dividends": round(sum(x["dividends"] for x in rows), 2),
        "gross_pf": round(gw / gl, 3) if gl else None,
        "net_pf": round(nw / nl, 3) if nl else None,
        "win_rate": round(sum(1 for x in rows if x["net"] > 0) / len(rows), 4),
        "mean_R": r.get("mean"),
        "t_clustered": r.get("t_clustered"),
        "ci95_R": r.get("ci95"),
        "clusters": r.get("clusters"),
    }


def equity_stats(curves: list[dict[Any, float]]) -> dict[str, Any]:
    """Max drawdown, Sharpe and Sortino (daily, annualized by sqrt(252)) of summed curves."""
    days = sorted(set().union(*[c.keys() for c in curves]))
    last = [None] * len(curves)
    total = []
    for d in days:
        for i, c in enumerate(curves):
            if d in c:
                last[i] = c[d]
        total.append(sum(x for x in last if x is not None))
    eq = np.asarray(total)
    rets = eq[1:] / eq[:-1] - 1
    peak = np.maximum.accumulate(eq)
    dd = float(((eq - peak) / peak).min()) if eq.size else 0.0
    sd = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
    down = rets[rets < 0]
    dsd = float(np.sqrt((down**2).mean())) if down.size else 0.0
    return {
        "start_equity": float(eq[0]),
        "end_equity": float(eq[-1]),
        "max_drawdown_pct": round(dd, 5),
        "sharpe": round(float(rets.mean()) / sd * math.sqrt(252), 3) if sd else None,
        "sortino": round(float(rets.mean()) / dsd * math.sqrt(252), 3) if dsd else None,
    }


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    artifacts: dict[str, Any] = {}
    stats: dict[str, Any] = {"per_config": {}, "pooled": {}, "combined_e2": {}}
    with factory() as session:
        regimes = spy_regimes(session, "sip")
        rows_by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        curves: dict[tuple[str, str], list[dict[Any, float]]] = defaultdict(list)
        for key in keys():
            entry = manifest.get(key, {})
            if entry.get("status") != "COMPLETED":
                artifacts[key] = {"status": entry.get("status", "NOT_RUN"), **entry}
                continue
            rid = run_id(session, manifest, key)
            run = session.get(BacktestRunRecord, rid)
            cfg_path = MANIFEST.parent / f"{key}.json"
            artifacts[key] = {
                "run_id": str(rid),
                "status": run.status.value,
                "engine_version": run.engine_version,
                "execution_conventions": run.config.get("execution_conventions"),
                "dividend_policy": run.config.get("dividend_policy"),
                "execution_model": f"{run.config['execution_model']['code']} "
                f"v{run.config['execution_model']['version']}",
                "execution_model_sha256": run.execution_model_sha256,
                "config_sha256": run.config_sha256,
                "request_file_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
                "data": f"{run.data_provider}/{run.data_feed}",
                "data_known_at": run.data_known_at.isoformat(),
                "dataset_sha256": run.dataset_sha256,
                "dataset_bars": run.dataset_bars,
                "result_sha256": run.result_sha256,
                "code_version": run.code_version,
                "seconds": entry.get("seconds"),
            }
            metrics = {
                m.metric: (None if m.value is None else float(m.value))
                for m in session.scalars(
                    select(BacktestMetricRecord).where(
                        BacktestMetricRecord.run_id == rid,
                        BacktestMetricRecord.scope == "PORTFOLIO",
                    )
                )
                if m.metric in PORTFOLIO
            }
            rows = trade_rows(session, rid, regimes)
            window, test, fam = key.split("--")
            stats["per_config"][key] = {"portfolio_metrics": metrics, "trades": summary(rows)}
            group = (test, fam)
            for r in rows:
                r["window"] = window
            rows_by[group] += rows
            curve = {
                s.session: float(s.equity)
                for s in session.scalars(
                    select(BacktestEquitySnapshotRecord).where(
                        BacktestEquitySnapshotRecord.run_id == rid
                    )
                )
            }
            curves[(test, window)].append(curve)
            # From the run's own equity curve (the engine stores Sharpe/Sortino only from 126
            # daily returns; a half-year window has about 124-128): short-sample estimates.
            stats["per_config"][key]["equity_curve"] = equity_stats([curve])
        for (test, fam), rows in sorted(rows_by.items()):
            by_window = {w: summary([r for r in rows if r["window"] == w]) for w in WINDOWS}
            by_year = {
                "2020": summary([r for r in rows if r["window"] == "a2020h2"]),
                "2021": summary([r for r in rows if r["window"] != "a2020h2"]),
            }
            by_regime = {}
            for dim in ("trend", "volatility"):
                for value in sorted({r[dim] for r in rows}):
                    by_regime[f"{dim}:{value}"] = summary([r for r in rows if r[dim] == value])
            by_direction = {
                d: summary([r for r in rows if r["direction"] == d]) for d in ("LONG", "SHORT")
            }
            exits = defaultdict(int)
            for r in rows:
                exits[r["exit"]] += 1
            stats["pooled"][f"{test}:{fam}"] = {
                "all": summary(rows),
                "by_window": by_window,
                "by_year": by_year,
                "by_regime": by_regime,
                "by_direction": by_direction,
                "exit_reasons": dict(exits),
            }
        for variant in ("primary", "old-conventions", "spread2x"):
            per_window = {
                w: equity_stats(curves[(f"e2-{variant}", w)])
                for w in WINDOWS
                if curves[(f"e2-{variant}", w)]
            }
            stats["combined_e2"][variant] = per_window
        stats["combined_e2"]["control"] = {
            w: equity_stats(curves[("control-primary", w)])
            for w in WINDOWS
            if curves[("control-primary", w)]
        }
    ART.write_text(json.dumps(artifacts, indent=1, default=str))
    OUT.write_text(json.dumps(stats, indent=1, default=str))
    done = sum(1 for a in artifacts.values() if a.get("status") == "COMPLETED")
    print(f"artifacts: {done}/{len(artifacts)} completed")
    for k, v in stats["pooled"].items():
        a = v["all"]
        print(k, a["n"], "net", a.get("net"), "gPF", a.get("gross_pf"), "nPF", a.get("net_pf"),
              "R", a.get("mean_R"), "CI", a.get("ci95_R"))  # fmt: skip


if __name__ == "__main__":
    main()

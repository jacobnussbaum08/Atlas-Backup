"""Evaluate experiment E1 exactly as pre-registered (read only).

Pre-registration: docs/phase-reports/e1-entry-order-preregistration.md (its SHA-256 is
recorded in the output). Control C = ``y20*--live-sip-measured--*`` runs; treatments T2
(primary), T1 (secondary), T0 (diagnostic) = ``y20*--e1-t{2,1,0}--*``. Writes
``runs/e1_results.json`` and prints the verdict. Nothing here changes a run.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from analyze import MANIFEST
from diagnose import clustered, load_closes, run_id
from scipy import stats as sps
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    BacktestProposalRecord,
    BacktestRunEventRecord,
    BacktestTradeRecord,
)
from app.database.sessions import build_engine, build_session_factory

ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "docs" / "phase-reports" / "e1-entry-order-preregistration.md"
OUT = MANIFEST.parent / "e1_results.json"
VARIANTS = {"C": "live-sip-measured", "T2": "e1-t2", "T1": "e1-t1", "T0": "e1-t0"}
WINDOWS = ("y2022", "y2023", "y2024", "y2025", "y2026h1")
STRATEGIES = (
    "breakout",
    "event_driven",
    "mean_reversion",
    "momentum",
    "trend_following",
    "volatility",
)
MIN_IMPROVEMENT_R = 0.08
MIN_T = 2.5
ALPHA = 0.05
MIN_MEASURED_SHARE = 0.90


def week(day: Any) -> tuple[int, int]:
    iso = day.isocalendar()
    return (iso[0], iso[1])


def collect(session: Any, manifest: dict, bars: dict, variant: str) -> dict[str, Any]:
    """Standalone trades, combined-run results, proposals and opening counts of a variant."""
    trades: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    combined: dict[str, Any] = {}
    opening: dict[str, int] = defaultdict(int)
    lookups: dict[str, int] = defaultdict(int)
    for window in WINDOWS:
        for scope in ("all6", *STRATEGIES):
            key = f"{window}--{variant}--{scope}"
            if manifest.get(key, {}).get("status") != "COMPLETED":
                raise LookupError(f"{key} has not completed")
            rid = run_id(session, manifest, key)
            props = {
                p.seq: p
                for p in session.scalars(
                    select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
                )
            }
            closed = list(
                session.scalars(
                    select(BacktestTradeRecord).where(
                        BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
                    )
                )
            )
            rows = []
            for t in closed:
                p = props[t.proposal_seq]
                risk = abs(float(t.entry_price) - float(p.invalidation_level)) * float(
                    t.entry_quantity
                )
                rows.append(
                    {
                        "strategy": t.strategy_id,
                        "window": window,
                        "week": week(p.session),
                        "R": float(t.net_pnl) / risk if risk else 0.0,
                        "net": float(t.net_pnl),
                        "entry_bar_stop": t.exit_reason == "STOP_LOSS" and t.holding_sessions == 0,
                    }
                )
            if scope == "all6":
                combined[window] = {
                    "trades": len(rows),
                    "net_pnl": round(sum(r["net"] for r in rows), 2),
                    "mean_R": round(float(np.mean([r["R"] for r in rows])), 4) if rows else None,
                }
                continue
            trades.extend(rows)
            for p in props.values():
                if p.risk_result != "PASS_FOR_RESERVATION":
                    continue
                fwd = forward5_atr(bars, p)
                if fwd is not None:
                    proposals.append(
                        {
                            "week": week(p.session),
                            "filled": p.trade_seq is not None,
                            "fwd5_atr": fwd,
                        }
                    )
            for e in session.scalars(
                select(BacktestRunEventRecord).where(
                    BacktestRunEventRecord.run_id == rid,
                    BacktestRunEventRecord.kind == "PIPELINE_SUMMARY",
                )
            ):
                entry = (e.details or {}).get("opening_entry") or {}
                for k, v in (entry.get("first_session") or {}).items():
                    opening[k] += int(v)
                for k, v in (entry.get("open_spread_lookups") or {}).items():
                    lookups[k] += int(v)
    return {
        "trades": trades,
        "proposals": proposals,
        "combined": combined,
        "opening": dict(opening),
        "open_spread_lookups": dict(lookups),
    }


def forward5_atr(bars: dict, p: Any) -> float | None:
    """Signed 5-session forward return from the signal close, in the strategy's ATR,
    measured on today's split-adjusted series (as in diagnose.py)."""
    days, series = bars[p.symbol]
    i = bisect_right(days, p.session) - 1
    if i < 0 or days[i] != p.session or i + 5 >= len(series):
        return None
    c0 = series[i]["c"]
    ref = float(p.reference_entry)
    atr_now = float(p.details["levels"]["atr"]) * c0 / ref
    s = 1 if p.direction == "LONG" else -1
    return s * (series[i + 5]["c"] - c0) / atr_now


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = clustered([r["R"] for r in rows], [r["week"] for r in rows])
    if out.get("n", 0) >= 2:
        x = np.asarray([r["R"] for r in rows])
        se = abs(out["mean"] / out["t_clustered"]) if out.get("t_clustered") else None
        out["se_clustered"] = None if se is None else round(se, 6)
        out["net_pnl"] = round(sum(r["net"] for r in rows), 2)
        out["std"] = round(float(x.std(ddof=1)), 4)
    return out


def compare(t: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    diff = t["mean"] - c["mean"]
    se = math.sqrt(t["se_clustered"] ** 2 + c["se_clustered"] ** 2)
    tstat = diff / se
    df = max(min(t["clusters"], c["clusters"]) - 1, 1)
    return {
        "difference_R": round(diff, 4),
        "se": round(se, 5),
        "t": round(tstat, 3),
        "p_one_sided": float(sps.t.sf(tstat, df)),
        "df": df,
    }


def holm(pvals: dict[str, float]) -> dict[str, bool]:
    ordered = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(ordered)
    passed: dict[str, bool] = {}
    still = True
    for rank, (name, p) in enumerate(ordered):
        still = still and p <= ALPHA / (m - rank)
        passed[name] = still
    return passed


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    with factory() as session:
        bars = load_closes(session)
        data = {name: collect(session, manifest, bars, v) for name, v in VARIANTS.items()}
    result: dict[str, Any] = {
        "preregistration": str(PREREG.relative_to(ROOT)),
        "preregistration_sha256": hashlib.sha256(PREREG.read_bytes()).hexdigest(),
        "variants": {},
    }
    for name, d in data.items():
        by_family = {s: summary([r for r in d["trades"] if r["strategy"] == s]) for s in STRATEGIES}
        approved = d["proposals"]
        filled = [p for p in approved if p["filled"]]
        stops = [r for r in d["trades"] if r["entry_bar_stop"]]
        result["variants"][name] = {
            "run_variant": VARIANTS[name],
            "pooled": summary(d["trades"]),
            "by_family": by_family,
            "combined_by_window": d["combined"],
            "approved_proposals": len(approved),
            "filled_proposals": len(filled),
            "selection": {
                "filled_fwd5_atr": clustered(
                    [p["fwd5_atr"] for p in filled], [p["week"] for p in filled]
                ),
                "approved_fwd5_atr": clustered(
                    [p["fwd5_atr"] for p in approved], [p["week"] for p in approved]
                ),
            },
            "entry_bar_stop_outs": {
                "count": len(stops),
                "net_pnl": round(sum(r["net"] for r in stops), 2),
            },
            "opening_first_session": d["opening"],
            "open_spread_lookups": d["open_spread_lookups"],
        }
    v = result["variants"]
    tests = {k: compare(v[k]["pooled"], v["C"]["pooled"]) for k in ("T2", "T1")}
    holm_pass = holm({k: t["p_one_sided"] for k, t in tests.items()})
    for k, t in tests.items():
        t["holm_pass"] = holm_pass[k]
        t["P1"] = t["difference_R"] >= MIN_IMPROVEMENT_R and t["t"] >= MIN_T and holm_pass[k]
    sel = v["T2"]["selection"]
    approved_mean = sel["approved_fwd5_atr"]["mean"]
    approved_se = (
        abs(approved_mean / sel["approved_fwd5_atr"]["t_clustered"])
        if sel["approved_fwd5_atr"].get("t_clustered")
        else None
    )
    gap = sel["filled_fwd5_atr"]["mean"] - approved_mean
    p2 = approved_se is not None and abs(gap) <= approved_se
    opening = v["T2"]["opening_first_session"]
    measured = opening.get("open_fill_measured", 0)
    total_open = measured + opening.get("open_fill_assumed", 0)
    share = measured / total_open if total_open else 0.0
    p3 = share >= MIN_MEASURED_SHARE
    result["tests"] = tests
    result["P2_selection_T2"] = {
        "filled_minus_approved_fwd5_atr": round(gap, 4),
        "approved_se": None if approved_se is None else round(approved_se, 4),
        "pass": p2,
    }
    result["P3_cost_coverage_T2"] = {
        "opening_fills": total_open,
        "measured_share": round(share, 4),
        "pass": p3,
    }
    p1 = tests["T2"]["P1"]
    result["verdict_T2_stage1"] = (
        "PASS"
        if p1 and p2 and p3
        else ("INCONCLUSIVE_ON_COSTS" if p1 and p2 and not p3 else "FAIL")
    )
    OUT.write_text(json.dumps(result, indent=1, default=str))
    keys = ("tests", "P2_selection_T2", "P3_cost_coverage_T2", "verdict_T2_stage1")
    print(json.dumps({k: result[k] for k in keys}, indent=1, default=str))
    for name in VARIANTS:
        p = v[name]["pooled"]
        print(name, "mean R", p["mean"], "t_cl", p.get("t_clustered"), "n", p["n"],
              "net", p.get("net_pnl"), "filled/approved",
              v[name]["filled_proposals"], "/", v[name]["approved_proposals"])  # fmt: skip


if __name__ == "__main__":
    main()

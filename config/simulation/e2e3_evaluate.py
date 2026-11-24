"""Evaluate E2/E3 stage A exactly as pre-registered (read only).

Pre-registration: docs/phase-reports/e2-e3-preregistration.md (its SHA-256 and this file's
are recorded in the output). Runs: ``a20*--{e2,e3,control}-{variant}--{strategy}``.
Writes runs/e2e3_stage_a_results.json and prints the verdicts. Nothing here changes a run.

Definitions (fixed in the pre-registration):

* R = net P&L (costs and dividends included) / (|entry fill - stop| x quantity), closed
  trades of the standalone runs, pooled over the three windows; standard errors clustered
  by ISO week of the signal.
* Gross profit factor = sum of positive gross P&L / |sum of negative gross P&L|.
* E3 excess = trade return (net P&L / entry notional) minus SPY's split-adjusted
  close-to-close return from the session before the entry session to the exit session.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from analyze import MANIFEST
from diagnose import clustered, load_closes, run_id
from scipy import stats as sps
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import BacktestProposalRecord, BacktestTradeRecord
from app.database.sessions import build_engine, build_session_factory

ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "docs" / "phase-reports" / "e2-e3-preregistration.md"
OUT = MANIFEST.parent / "e2e3_stage_a_results.json"
WINDOWS = ("a2020h2", "a2021h1", "a2021h2")
E2_FAMILIES = ("momentum", "breakout", "trend_following")
VARIANTS = ("primary", "old-conventions", "spread2x")
MIN_TRADES = 60  # below this a family is INSUFFICIENT (not rejected, not continued)
ALPHA = 0.05


def trades_of(session: Any, manifest: dict, key: str, closes: dict) -> list[dict[str, Any]]:
    if manifest.get(key, {}).get("status") != "COMPLETED":
        raise LookupError(f"{key} has not completed")
    rid = run_id(session, manifest, key)
    props = {
        p.seq: p
        for p in session.scalars(
            select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
        )
    }
    out = []
    spy_days, spy = closes["SPY"]
    for t in session.scalars(
        select(BacktestTradeRecord).where(
            BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
        )
    ):
        p = props[t.proposal_seq]
        entry, qty = float(t.entry_price), float(t.entry_quantity)
        risk = abs(entry - float(p.invalidation_level)) * qty
        net = float(t.net_pnl)
        first = t.details["first_session"]
        last = t.details["last_session"]
        i0 = bisect_left(spy_days, date.fromisoformat(first)) - 1
        i1 = bisect_left(spy_days, date.fromisoformat(last))
        spy_ret = spy[i1]["c"] / spy[i0]["c"] - 1 if i0 >= 0 and i1 < len(spy) else math.nan
        ret = net / (entry * qty) if entry * qty else 0.0
        iso = p.session.isocalendar()
        out.append(
            {
                "R": net / risk if risk else 0.0,
                "net": net,
                "gross": float(t.realized_gross),
                "dividends": float(t.details.get("dividends", 0) or 0),
                "week": (iso[0], iso[1]),
                "window": key.split("--")[0],
                "excess": ret - spy_ret if t.direction == "LONG" else -ret - spy_ret,
            }
        )
    return out


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    if len(rows) < 2:
        return out
    c = clustered([r["R"] for r in rows], [r["week"] for r in rows])
    t = c.get("t_clustered")
    df = max(int(c.get("clusters", 2)) - 1, 1)
    wins = sum(r["gross"] for r in rows if r["gross"] > 0)
    losses = -sum(r["gross"] for r in rows if r["gross"] < 0)
    by_window = defaultdict(list)
    for r in rows:
        by_window[r["window"]].append(r["R"])
    ex = [r["excess"] for r in rows if not math.isnan(r["excess"])]
    ce = clustered(ex, [r["week"] for r in rows if not math.isnan(r["excess"])]) if ex else {}
    out.update(
        mean_R=c["mean"],
        t_clustered=t,
        p_one_sided_positive=None if t is None else float(sps.t.sf(t, df)),
        p_one_sided_negative=None if t is None else float(sps.t.cdf(t, df)),
        gross_pf=wins / losses if losses else None,
        net=sum(r["net"] for r in rows),
        dividends=sum(r["dividends"] for r in rows),
        windows_positive=sum(1 for v in by_window.values() if sum(v) / len(v) > 0),
        windows=len(by_window),
        excess_mean=ce.get("mean"),
        excess_t=ce.get("t_clustered"),
    )
    return out


def verdict(primary: dict[str, Any], stress: dict[str, Any], e3: bool) -> str:
    """Stage-A outcome, as pre-registered (falsification only)."""
    if primary["n"] < MIN_TRADES:
        return "INSUFFICIENT"
    m, pf = primary["mean_R"], primary.get("gross_pf") or 0.0
    if primary.get("p_one_sided_negative") is not None and primary["p_one_sided_negative"] < ALPHA:
        return "REJECTED"
    if m <= 0 or pf < 1.0:
        return "REJECTED"
    if e3:
        ok = m > 0.05 and (primary.get("excess_mean") or 0) > 0 and stress.get("mean_R", -1) > 0
    else:
        ok = pf > 1.1 and stress.get("mean_R", -1) > 0
    return "CONTINUE_TO_STAGE_B" if ok else "NOT_SUPPORTED"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    result: dict[str, Any] = {
        "preregistration_sha256": hashlib.sha256(PREREG.read_bytes()).hexdigest(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tests": {},
        "reporting_only": {},
    }
    plan = [("e2", f) for f in E2_FAMILIES] + [("e3", "momentum")]
    with factory() as session:
        closes = load_closes(session)
        for exp, fam in plan:
            per = {}
            for v in VARIANTS:
                rows = []
                for w in WINDOWS:
                    rows += trades_of(session, manifest, f"{w}--{exp}-{v}--{fam}", closes)
                per[v] = stats(rows)
            per["verdict"] = verdict(per["primary"], per["spread2x"], exp == "e3")
            result["tests"][f"{exp}:{fam}"] = per
        for fam in E2_FAMILIES:
            rows = []
            for w in WINDOWS:
                rows += trades_of(session, manifest, f"{w}--control-primary--{fam}", closes)
            result["reporting_only"][f"control:{fam}"] = stats(rows)
    OUT.write_text(json.dumps(result, indent=1, default=str))
    for k, v in result["tests"].items():
        p = v["primary"]
        print(k, v["verdict"], "n", p["n"], "R", p.get("mean_R"), "t", p.get("t_clustered"),
              "gPF", p.get("gross_pf"), "old R", v["old-conventions"].get("mean_R"),
              "2x R", v["spread2x"].get("mean_R"))  # fmt: skip


if __name__ == "__main__":
    main()

"""E7: baseline vs corrected execution conventions on the control runs (read only).

Baseline = ``y20*--live-sip-measured--*`` (immutable, earlier runs). Corrected =
``y20*--e7-corrected--*``; attribution = ``e7-limit``, ``e7-stop``, ``e7-market`` (one
correction each). Same strategies, risk, entries and data. Writes runs/e7_results.json.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from analyze import MANIFEST
from diagnose import load_closes, run_id
from e1_evaluate import STRATEGIES, WINDOWS, collect, compare, summary
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import BacktestRunEventRecord, BacktestTradeRecord
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "e7_results.json"
VARIANTS = {
    "baseline": "live-sip-measured",
    "corrected": "e7-corrected",
    "limit_only": "e7-limit",
    "stop_only": "e7-stop",
    "market_only": "e7-market",
}


def money(session: Any, manifest: dict, variant: str) -> dict[str, Any]:
    """Gross, costs and net of closed trades; standalone pooled, by strategy and all6."""
    out: dict[str, Any] = {"standalone": defaultdict(lambda: defaultdict(float)), "all6": {}}
    fidelity: dict[str, int] = defaultdict(int)
    for window in WINDOWS:
        for scope in ("all6", *STRATEGIES):
            key = f"{window}--{variant}--{scope}"
            rid = run_id(session, manifest, key)
            trades = list(
                session.scalars(
                    select(BacktestTradeRecord).where(
                        BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
                    )
                )
            )
            gross = sum(float(t.realized_gross) for t in trades)
            costs = sum(float(t.costs) for t in trades)
            net = sum(float(t.net_pnl) for t in trades)
            wins = sum(float(t.realized_gross) for t in trades if float(t.realized_gross) > 0)
            losses = -sum(float(t.realized_gross) for t in trades if float(t.realized_gross) < 0)
            if scope == "all6":
                out["all6"][window] = {
                    "trades": len(trades),
                    "gross": round(gross, 2),
                    "costs": round(costs, 2),
                    "net": round(net, 2),
                    "gross_pf": round(wins / losses, 3) if losses else None,
                }
                continue
            s = out["standalone"][scope]
            s["trades"] += len(trades)
            s["gross"] += gross
            s["costs"] += costs
            s["net"] += net
            s["gross_wins"] += wins
            s["gross_losses"] += losses
            for e in session.scalars(
                select(BacktestRunEventRecord).where(
                    BacktestRunEventRecord.run_id == rid,
                    BacktestRunEventRecord.kind == "PIPELINE_SUMMARY",
                )
            ):
                ef = (e.details or {}).get("execution_fidelity") or {}
                for k, v in (ef.get("corrected_fills") or {}).items():
                    fidelity[k] += int(v)
    std = out["standalone"]
    total = defaultdict(float)
    for s in std.values():
        for k, v in s.items():
            total[k] += v
    for s in [*std.values(), total]:
        s["gross_pf"] = s["gross_wins"] / s["gross_losses"] if s["gross_losses"] else None
    out["standalone"] = {k: {kk: round(vv, 3) for kk, vv in v.items() if vv is not None}
                         for k, v in std.items()}  # fmt: skip
    out["standalone_total"] = {k: round(v, 3) for k, v in total.items() if v is not None}
    out["corrected_fills"] = dict(fidelity)
    return out


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    result: dict[str, Any] = {"variants": {}}
    with factory() as session:
        bars = load_closes(session)
        for name, variant in VARIANTS.items():
            d = collect(session, manifest, bars, variant)
            by_family = {
                s: summary([r for r in d["trades"] if r["strategy"] == s]) for s in STRATEGIES
            }
            stops = [r for r in d["trades"] if r["entry_bar_stop"]]
            result["variants"][name] = {
                "run_variant": variant,
                "pooled_R": summary(d["trades"]),
                "by_family_R": by_family,
                "combined_by_window": d["combined"],
                "entry_bar_stop_outs": {
                    "count": len(stops),
                    "net_pnl": round(sum(r["net"] for r in stops), 2),
                },
                "money": money(session, manifest, variant),
                "_trades": d["trades"],
            }
    v = result["variants"]
    result["corrected_vs_baseline_R"] = compare(
        v["corrected"]["pooled_R"], v["baseline"]["pooled_R"]
    )
    for name in v:
        v[name].pop("_trades")
    OUT.write_text(json.dumps(result, indent=1, default=str))
    for name, x in v.items():
        p, m = x["pooled_R"], x["money"]["standalone_total"]
        print(name, "R", p["mean"], "t_cl", p.get("t_clustered"), "n", p["n"],
              "net", round(m["net"]), "gross", round(m["gross"]), "costs", round(m["costs"]),
              "gPF", round(m["gross_pf"], 3), "EBS", x["entry_bar_stop_outs"])  # fmt: skip
        print("   all6", {w: (c["net"], c["gross_pf"]) for w, c in x["money"]["all6"].items()})
        print("   fam", {s: round(f["net"]) for s, f in x["money"]["standalone"].items()})
    print("corrected vs baseline", result["corrected_vs_baseline_R"])
    print("corrected fills", v["corrected"]["money"]["corrected_fills"])


if __name__ == "__main__":
    main()

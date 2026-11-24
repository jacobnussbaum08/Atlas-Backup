"""E2/E3 stage A: why mean R and dollar P&L disagree in sign (read only, descriptive).

Reports, per stage-A primary test: the dollar expectancy per trade with its week-clustered t,
the R distribution (median, quantiles, the most negative values), the dollar risk at entry
(|entry fill - stop| x quantity) and how it relates to R, and the trades whose entry filled
close to the stop. Decides nothing: the pre-registered verdicts use mean R as defined.
Writes runs/e2e3_r_diagnostics.json.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from analyze import MANIFEST
from diagnose import clustered, run_id
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import BacktestProposalRecord, BacktestTradeRecord
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "e2e3_r_diagnostics.json"
WINDOWS = ("a2020h2", "a2021h1", "a2021h2")
TESTS = [("e2", "momentum"), ("e2", "breakout"), ("e2", "trend_following"), ("e3", "momentum")]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    out: dict[str, Any] = {}
    with factory() as session:
        for exp, fam in TESTS:
            rows = []
            for w in WINDOWS:
                rid = run_id(session, manifest, f"{w}--{exp}-primary--{fam}")
                props = {
                    p.seq: p
                    for p in session.scalars(
                        select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
                    )
                }
                for t in session.scalars(
                    select(BacktestTradeRecord).where(
                        BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
                    )
                ):
                    p = props[t.proposal_seq]
                    entry, qty = float(t.entry_price), float(t.entry_quantity)
                    planned = abs(float(p.reference_entry) - float(p.invalidation_level))
                    actual = abs(entry - float(p.invalidation_level))
                    risk = actual * qty
                    iso = p.session.isocalendar()
                    rows.append(
                        {
                            "symbol": t.symbol,
                            "session": str(p.session),
                            "net": float(t.net_pnl),
                            "risk_usd": risk,
                            "R": float(t.net_pnl) / risk if risk else 0.0,
                            "stop_distance_vs_planned": actual / planned if planned else None,
                            "week": (iso[0], iso[1]),
                            "exit": t.exit_reason,
                        }
                    )
            if len(rows) < 2:
                out[f"{exp}:{fam}"] = {"n": len(rows)}
                continue
            r = np.asarray([x["R"] for x in rows])
            usd = clustered([x["net"] for x in rows], [x["week"] for x in rows])
            shrunk = [x for x in rows if (x["stop_distance_vs_planned"] or 1) < 0.5]
            corr = float(np.corrcoef([x["risk_usd"] for x in rows], r)[0, 1])
            out[f"{exp}:{fam}"] = {
                "n": len(rows),
                "dollar_expectancy": usd.get("mean"),
                "dollar_t_clustered": usd.get("t_clustered"),
                "mean_R": float(r.mean()),
                "median_R": float(np.median(r)),
                "R_quantiles_5_25_75_95": [float(q) for q in np.percentile(r, [5, 25, 75, 95])],
                "most_negative_R": sorted(
                    (
                        {
                            k: x[k]
                            for k in (
                                "symbol",
                                "session",
                                "R",
                                "net",
                                "risk_usd",
                                "stop_distance_vs_planned",
                                "exit",
                            )
                        }
                        for x in rows
                    ),
                    key=lambda x: x["R"],
                )[:5],
                "risk_usd_min_median_max": [
                    float(np.min([x["risk_usd"] for x in rows])),
                    float(np.median([x["risk_usd"] for x in rows])),
                    float(np.max([x["risk_usd"] for x in rows])),
                ],
                "corr_risk_usd_vs_R": corr,
                "entries_with_stop_distance_below_half_of_planned": {
                    "count": len(shrunk),
                    "sum_R": float(sum(x["R"] for x in shrunk)),
                    "sum_net": float(sum(x["net"] for x in shrunk)),
                },
                "mean_R_excluding_those": float(np.mean([x["R"] for x in rows if x not in shrunk])),
            }
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

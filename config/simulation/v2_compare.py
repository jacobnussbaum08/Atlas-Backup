"""Engine 8A.2 default (daily-v2 + dividends) vs the E7-corrected option vs the baseline.

Read only. ``v2-default`` and ``e7-corrected`` apply the same execution conventions, so their
fills must be identical; their trades differ only by dividend cash (and anything that cash
changes downstream: sizing from equity, capacity). Writes runs/v2_compare.json.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from analyze import MANIFEST
from diagnose import run_id
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import BacktestFillRecord, BacktestTradeRecord
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "v2_compare.json"
WINDOWS = ("y2022", "y2023", "y2024", "y2025", "y2026h1")
SCOPES = ("all6", "breakout", "event_driven", "mean_reversion", "momentum",
          "trend_following", "volatility")  # fmt: skip
VARIANTS = ("live-sip-measured", "e7-corrected", "v2-default")


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    out: dict[str, Any] = {"standalone": {}, "all6": {}, "fills_identical": {}}
    with factory() as session:
        fills: dict[tuple[str, str], list[tuple]] = {}
        for v in VARIANTS:
            std = defaultdict(float)
            fam: dict[str, float] = defaultdict(float)
            for w in WINDOWS:
                for scope in SCOPES:
                    rid = run_id(session, manifest, f"{w}--{v}--{scope}")
                    closed = list(
                        session.scalars(
                            select(BacktestTradeRecord).where(
                                BacktestTradeRecord.run_id == rid,
                                BacktestTradeRecord.status == "CLOSED",
                            )
                        )
                    )
                    net = sum(float(t.net_pnl) for t in closed)
                    gross = sum(float(t.realized_gross) for t in closed)
                    div = sum(float(t.details.get("dividends", 0) or 0) for t in closed)
                    if scope == "all6":
                        out["all6"].setdefault(v, {})[w] = {
                            "trades": len(closed), "net": round(net, 2), "dividends": round(div, 2)
                        }  # fmt: skip
                        continue
                    std["trades"] += len(closed)
                    std["net"] += net
                    std["gross"] += gross
                    std["dividends"] += div
                    fam[scope] += net
                    fills[(v, f"{w}--{scope}")] = [
                        (f.session, f.symbol, f.role, str(f.price), str(f.quantity))
                        for f in session.scalars(
                            select(BacktestFillRecord)
                            .where(BacktestFillRecord.run_id == rid)
                            .order_by(BacktestFillRecord.seq)
                        )
                    ]
            out["standalone"][v] = {
                **{k: round(x, 2) for k, x in std.items()},
                "by_family_net": {k: round(x, 2) for k, x in fam.items()},
            }
    same = differ = 0
    for (v, key), rows in fills.items():
        if v != "v2-default":
            continue
        if rows == fills[("e7-corrected", key)]:
            same += 1
        else:
            differ += 1
    out["fills_identical"] = {"runs_identical": same, "runs_different": differ}
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

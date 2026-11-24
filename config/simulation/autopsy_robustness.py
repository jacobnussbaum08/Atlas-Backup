"""Autopsy robustness: the signal forward returns with coarser clusters (read only).

10- and 20-session forward windows overlap across weeks, so week-clustered t-statistics
overstate significance. This recomputes the SEEN signal statistics clustered by calendar
month and by calendar quarter, and reports how many (family, direction, horizon, measure)
cells were examined, for a multiple-testing reading. Writes runs/autopsy_robustness.json.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date
from typing import Any

from analyze import MANIFEST
from autopsy import FAMILIES, SEEN_WINDOWS, bars, signals_for
from diagnose import clustered

from app.core.config import get_settings
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "autopsy_robustness.json"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    rows: list[dict[str, Any]] = []
    with factory() as session:
        b = bars(session)
        for w in SEEN_WINDOWS:
            for f in FAMILIES:
                rows += signals_for(session, manifest, f"{w}--v2-default--{f}", b)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"{r['family']}:{r['dir']}"].append(r)
    out: dict[str, Any] = {"cells_examined": 0, "groups": {}}
    for k, g in sorted(groups.items()):
        res: dict[str, Any] = {"n": len(g)}
        for m in ("f10_atr", "f20_atr", "x10_pct", "x20_pct"):
            vals = [(r[m], r["week"]) for r in g if not math.isnan(r[m])]
            if len(vals) < 2:
                continue
            v = [x for x, _ in vals]
            # ISO (year, week) -> month and quarter of that week's Monday.

            months = [date.fromisocalendar(y, wk, 1) for _, (y, wk) in vals]
            res[m] = {
                "mean": round(sum(v) / len(v), 4),
                "t_week": clustered(v, [w for _, w in vals]).get("t_clustered"),
                "t_month": clustered(v, [(d.year, d.month) for d in months]).get("t_clustered"),
                "t_quarter": clustered(v, [(d.year, (d.month - 1) // 3) for d in months]).get(
                    "t_clustered"
                ),
            }
        out["groups"][k] = res
    # The autopsy examined 12 family-direction groups x 4 horizons x 2 measures = 96 cells.
    out["cells_examined"] = len(groups) * 4 * 2
    out["bonferroni_t_for_alpha_0.05_two_sided"] = 3.5
    OUT.write_text(json.dumps(out, indent=1))
    for k, v in out["groups"].items():
        print(k, v["n"], {m: v[m] for m in v if m != "n"})


if __name__ == "__main__":
    main()

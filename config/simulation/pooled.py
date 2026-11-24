"""Pool closed trades across the independent yearly windows (``y20*`` runs) per strategy
and regime, and write ``runs/pooled.json``. Reads stored results only (D-234).

Each yearly window is a fresh account, so trades from different windows are independent
draws of the same rules (same stored parameters, same risk limits). Pooling them gives the
largest out-of-sample trade sample the simulation has without restarting mid-year.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import Any

from analyze import MANIFEST, MIN_TRADES, reg_zone, spy_regimes  # type: ignore[import-not-found]
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import BacktestTradeRecord
from app.database.sessions import build_engine, build_session_factory
from app.learning import regimes as reg
from app.learning.statistics import t_test_mean

OUT = MANIFEST.parent / "pooled.json"


def stats(trades: list[Any]) -> dict[str, Any]:
    pnls = [float(t.net_pnl) for t in trades]
    gross = [float(t.realized_gross) + float(t.unrealized) for t in trades]
    costs = sum(float(t.costs) for t in trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    t_stat, p = t_test_mean(pnls) if len(pnls) > 2 else (None, None)
    return {
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "gross_pnl": round(sum(gross), 2),
        "costs": round(costs, 2),
        "avg_net_trade": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "t": None if t_stat is None else round(t_stat, 3),
        "p_one_sided_mean_le_0": None if p is None else round(p, 4),
        "sample": "SUFFICIENT" if len(pnls) >= MIN_TRADES else "INSUFFICIENT",
    }


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    out: dict[str, Any] = {}
    with factory() as session:
        regimes = spy_regimes(session, "sip")

        def regime_of(t: Any) -> reg.Regime:
            return reg.known_before(regimes, t.opened_at.astimezone(reg_zone()).date())

        groups: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
        for key, entry in sorted(manifest.items()):
            if not key.startswith("y20") or entry.get("status") != "COMPLETED":
                continue
            window, _variant, scope = key.split("--")
            closed = list(
                session.scalars(
                    select(BacktestTradeRecord).where(
                        BacktestTradeRecord.run_id == uuid.UUID(entry["run_id"]),
                        BacktestTradeRecord.status == "CLOSED",
                    )
                )
            )
            account = "combined" if scope == "all6" else "standalone"
            for t in closed:
                g = regime_of(t)
                groups[f"{account}:strategy"][t.strategy_id].append(t)
                groups[f"{account}:window"][f"{window}:{t.strategy_id}"].append(t)
                groups[f"{account}:trend"][f"{t.strategy_id}:{g.trend}"].append(t)
                groups[f"{account}:volatility"][f"{t.strategy_id}:{g.volatility}"].append(t)
                groups[f"{account}:direction"][f"{t.strategy_id}:{t.direction}"].append(t)
                groups[f"{account}:exit"][f"{t.strategy_id}:{t.exit_reason}"].append(t)
                if account == "combined":
                    groups["combined:portfolio"]["all"].append(t)
                    groups["combined:portfolio_window"][window].append(t)
                    groups["combined:portfolio_trend"][g.trend].append(t)
                    groups["combined:portfolio_volatility"][g.volatility].append(t)
        for name, buckets in sorted(groups.items()):
            out[name] = {k: stats(v) for k, v in sorted(buckets.items())}
    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

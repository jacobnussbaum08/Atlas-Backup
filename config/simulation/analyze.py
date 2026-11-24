"""Summarize completed simulation runs into ``runs/results.json`` (D-234).

Reads only stored run results from the simulation database (``.env.sim``) and reuses the
8B statistics (stationary block bootstrap, trade Monte Carlo) and regime labels
(``app.learning.regimes``: SPY 50-session trend, 20-session realized volatility vs its
own median). Nothing here changes a run.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    Asset,
    BacktestBenchmarkResultRecord,
    BacktestEquitySnapshotRecord,
    BacktestMetricRecord,
    BacktestRejectionRecord,
    BacktestRunEventRecord,
    BacktestRunRecord,
    BacktestTradeRecord,
    MarketBarRecord,
)
from app.database.sessions import build_engine, build_session_factory
from app.learning import regimes as reg
from app.learning.statistics import block_bootstrap, monte_carlo_trades, t_test_mean

HERE = Path(__file__).parent
MANIFEST = HERE / "runs" / "manifest.json"
OUT = HERE / "runs" / "results.json"
MIN_TRADES = 30  # the 11A protocol's per-strategy minimum
PORTFOLIO_KEYS = (
    "total_return",
    "gross_total_return",
    "annualized_return",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown_pct",
    "win_rate",
    "profit_factor",
    "expectancy",
    "trades_closed",
    "trades_open_at_end",
    "total_costs",
    "net_pnl",
    "proposals",
    "risk_approved",
    "entries_filled",
    "turnover",
)


def f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def spy_regimes(session: Any, feed: str) -> dict[date, reg.Regime]:
    spy = session.scalar(select(Asset.id).where(Asset.symbol == "SPY"))
    b = MarketBarRecord
    rows = session.execute(
        select(b.event_time, b.close, b.revision)
        .where(
            b.asset_id == spy,
            b.feed == feed,
            b.timeframe == "1Day",
            b.adjustment == "SPLIT",
        )
        .order_by(b.event_time, b.revision)
    ).all()
    closes: dict[date, float] = {}
    for at, close, _rev in rows:
        # Daily bars are stamped at local midnight (UTC 04:00/05:00): that date is the session.
        closes[at.astimezone(reg_zone()).date()] = float(close)
    return reg.classify(sorted(closes.items()))


def reg_zone() -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def group_trades(trades: list[Any], key: Any) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(float(t.net_pnl))
    out = {}
    for k, pnls in sorted(groups.items()):
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        out[k] = {
            "trades": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "avg_net_trade": round(sum(pnls) / len(pnls), 2),
            "win_rate": round(len(wins) / len(pnls), 4),
            "profit_factor": None if not losses else round(sum(wins) / -sum(losses), 3),
            "sample": "SUFFICIENT" if len(pnls) >= MIN_TRADES else "INSUFFICIENT",
        }
    return out


def analyze_run(session: Any, key: str, run_id: uuid.UUID, regimes_by_feed: dict) -> dict:
    run = session.get(BacktestRunRecord, run_id)
    if run is None:
        raise LookupError(f"run {run_id} is not in this database")
    feed = run.config["data"]["feed"]
    if feed not in regimes_by_feed:
        regimes_by_feed[feed] = spy_regimes(session, feed)
    regimes = regimes_by_feed[feed]
    metrics: dict[str, dict[str, Any]] = defaultdict(dict)
    stored = select(BacktestMetricRecord).where(BacktestMetricRecord.run_id == run_id)
    for m in session.scalars(stored):
        if m.scope in ("PORTFOLIO", "STRATEGY", "DIRECTION"):
            metrics[f"{m.scope}:{m.scope_key}"][m.metric] = (
                f(m.value) if m.status == "OK" else m.status
            )
    portfolio = {k: metrics["PORTFOLIO:portfolio"].get(k) for k in PORTFOLIO_KEYS}
    snaps = list(
        session.scalars(
            select(BacktestEquitySnapshotRecord)
            .where(BacktestEquitySnapshotRecord.run_id == run_id)
            .order_by(BacktestEquitySnapshotRecord.session)
        )
    )
    capital = float(run.config["initial_capital"])
    equity = [capital] + [float(s.equity) for s in snaps]
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    days = [s.session for s in snaps]
    trades = list(
        session.scalars(select(BacktestTradeRecord).where(BacktestTradeRecord.run_id == run_id))
    )
    closed = [t for t in trades if t.status == "CLOSED"]

    def regime_of(t: Any) -> reg.Regime:
        return reg.known_before(regimes, t.opened_at.astimezone(reg_zone()).date())

    by_ret_regime: dict[str, list[float]] = defaultdict(list)
    for d, r in zip(days, rets, strict=True):
        g = reg.known_before(regimes, d)
        by_ret_regime[f"trend:{g.trend}"].append(r)
        by_ret_regime[f"vol:{g.volatility}"].append(r)
    session_regimes = {
        k: {
            "sessions": len(v),
            "compounded_return": round(math.prod(1 + x for x in v) - 1, 5),
            "mean_daily_return": round(sum(v) / len(v), 6),
        }
        for k, v in sorted(by_ret_regime.items())
    }
    bench = {
        b.kind: {"label": b.label, "total_return": f(b.total_return)}
        for b in session.scalars(
            select(BacktestBenchmarkResultRecord).where(
                BacktestBenchmarkResultRecord.run_id == run_id
            )
        )
        if b.available
    }
    rejections: dict[str, int] = defaultdict(int)
    for r in session.scalars(
        select(BacktestRejectionRecord).where(BacktestRejectionRecord.run_id == run_id)
    ):
        rejections[f"{r.stage}:{r.code}"] += 1
    pipeline = next(
        (
            e.details
            for e in session.scalars(
                select(BacktestRunEventRecord).where(
                    BacktestRunEventRecord.run_id == run_id,
                    BacktestRunEventRecord.kind == "PIPELINE_SUMMARY",
                )
            )
        ),
        None,
    )
    pnls = [float(t.net_pnl) for t in closed]
    t_stat, p_value = t_test_mean(pnls) if len(pnls) > 2 else (None, None)
    year_of = lambda t: str(t.opened_at.year)  # noqa: E731
    return {
        "key": key,
        "run_id": str(run_id),
        "name": run.name,
        "period": [str(run.config["start_date"]), str(run.config["end_date"])],
        "initial_capital": capital,
        "feed": feed,
        "pipeline": run.config.get("pipeline"),
        "execution_overrides": run.config["execution"].get("equity_overrides"),
        "result_sha256": run.result_sha256,
        "portfolio": portfolio,
        "avg_net_trade": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "trade_t_test": {"t": t_stat, "p_one_sided_mean_le_0": p_value},
        "sample": "SUFFICIENT" if len(closed) >= MIN_TRADES else "INSUFFICIENT",
        "by_strategy": {
            k.split(":", 1)[1]: v for k, v in metrics.items() if k.startswith("STRATEGY:")
        },
        "by_direction": group_trades(closed, lambda t: t.direction),
        "by_trend_regime": group_trades(closed, lambda t: regime_of(t).trend),
        "by_volatility_regime": group_trades(closed, lambda t: regime_of(t).volatility),
        "by_year": group_trades(closed, year_of),
        "session_returns_by_regime": session_regimes,
        "bootstrap": block_bootstrap(rets, mean_block=10, resamples=2000, seed=7)
        if len(rets) > 60
        else None,
        "monte_carlo": monte_carlo_trades(pnls, capital, resamples=2000, seed=7)
        if len(pnls) >= 10
        else None,
        "benchmarks": bench,
        "rejections": dict(sorted(rejections.items(), key=lambda kv: -kv[1])[:25]),
        "pipeline_summary": pipeline,
        "equity_curve": [[str(d), round(e, 2)] for d, e in zip(days, equity[1:], strict=True)],
    }


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    engine = build_engine(get_settings())
    factory = build_session_factory(engine)
    results = {}
    regimes_by_feed: dict = {}
    with factory() as session:
        for key, entry in sorted(manifest.items()):
            run_id = entry.get("run_id")
            if entry.get("status") == "COMPLETED" and not run_id:
                # Early runner versions missed the id: the newest COMPLETED run of the
                # configuration's unique name is the one the manifest recorded.
                run_id = session.scalar(
                    select(BacktestRunRecord.id)
                    .where(
                        BacktestRunRecord.name == f"sim {key}",
                        BacktestRunRecord.status == "COMPLETED",
                    )
                    .order_by(BacktestRunRecord.created_at.desc())
                    .limit(1)
                )
            if entry.get("status") != "COMPLETED" or not run_id:
                results[key] = {"key": key, "status": entry.get("status")}
                continue
            results[key] = analyze_run(session, key, uuid.UUID(str(run_id)), regimes_by_feed)
    OUT.write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {len(results)} run summaries to {OUT}")


if __name__ == "__main__":
    main()

"""E7 static audit: re-price every fill of the control runs under corrected conventions (read only).

Control = ``y20*--live-sip-measured--*`` (5 yearly windows x all6 + 6 standalone). Holding
paths are kept as they happened (same trades, same quantities, same sessions); only the
price each fill would have received under the corrected convention changes. The dynamic
effect (different paths, capacity, breakers) is measured by the corrected re-runs instead.

Corrected conventions (see docs/phase-reports/e7-simulation-accuracy-audit.md):

* A1 limit opened through: a limit (entry or take-profit) whose session opened better than
  its limit fills at the open, adjusted adversely by the measured opening half spread and the
  model slippage, never worse than the limit (baseline: at the limit).
* A2 stop, no gap: fills at the stop adjusted adversely by the session's measured half
  spread and slippage (baseline: the worse of the stop, at zero cost, and the close with
  costs).
* A3 stop, gap: fills at the open adjusted adversely by the opening half spread and slippage
  (baseline: the worst of the stop, the open and the close).
* A4 market exit (holding horizon): an order submitted after the close fills at the next
  open adjusted by the opening half spread and slippage (baseline: that session's close).
* A5 a stop that fired in the session a horizon exit was already working, without a gap:
  the exit executed at the open first (baseline: the stop).

Every price stays inside the session's bar. Writes runs/e7_static.json.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from analyze import MANIFEST, reg_zone
from diagnose import run_id
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    AssetMetricValueRecord,
    BacktestFillRecord,
    BacktestOrderRecord,
    BacktestTradeRecord,
    MarketBarRecord,
)
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "e7_static.json"
WINDOWS = ("y2022", "y2023", "y2024", "y2025", "y2026h1")
SCOPES = (
    "all6",
    "breakout",
    "event_driven",
    "mean_reversion",
    "momentum",
    "trend_following",
    "volatility",
)
SLIP = Decimal(2)  # model slippage, bps
ASSUMED_HALF = Decimal(5)
BPS = Decimal(10000)
TICK = Decimal("0.01")
BUYS = {"BUY", "BUY_TO_COVER"}


def adverse(value: Decimal, buys: bool) -> Decimal:
    step = (value / TICK).to_integral_value(rounding=ROUND_CEILING if buys else ROUND_FLOOR)
    return step * TICK


def clamp(p: Decimal, bar: dict[str, Decimal]) -> Decimal:
    return min(max(p, bar["l"]), bar["h"])


def at_open(bar: dict[str, Decimal], half: Decimal, buys: bool) -> Decimal:
    adj = (half + SLIP) / BPS
    return clamp(adverse(bar["o"] * (1 + adj if buys else 1 - adj), buys), bar)


def load_bars(session: Any) -> dict[tuple[uuid.UUID, date], dict[str, Decimal]]:
    zone = reg_zone()
    b = MarketBarRecord
    out: dict[tuple[uuid.UUID, date], dict[str, Decimal]] = {}
    for aid, at, o, h, lo, c in session.execute(
        select(b.asset_id, b.event_time, b.open, b.high, b.low, b.close)
        .where(b.feed == "sip", b.timeframe == "1Day", b.adjustment == "RAW")
        .order_by(b.asset_id, b.event_time, b.revision)
    ):
        out[(aid, at.astimezone(zone).date())] = {"o": o, "h": h, "l": lo, "c": c}
    return out


def load_spreads(session: Any, version: str) -> dict[tuple[uuid.UUID, date], Decimal]:
    zone = reg_zone()
    m = AssetMetricValueRecord
    out = {}
    for aid, at, value in session.execute(
        select(m.asset_id, m.as_of, m.value).where(
            m.metric == "quoted_spread_bps", m.feed == "sip", m.feature_version == version
        )
    ):
        if value is not None:
            out[(aid, at.astimezone(zone).date())] = Decimal(value) / 2
    return out


def audit_run(
    session: Any,
    rid: uuid.UUID,
    bars: dict,
    open_half: dict,
    close_half: dict,
) -> dict[str, Any]:
    orders = {
        o.seq: o
        for o in session.scalars(
            select(BacktestOrderRecord).where(BacktestOrderRecord.run_id == rid)
        )
    }
    trades = {
        t.seq: t
        for t in session.scalars(
            select(BacktestTradeRecord).where(BacktestTradeRecord.run_id == rid)
        )
    }
    by_trade: dict[int, list[Any]] = defaultdict(list)
    for o in orders.values():
        if o.trade_seq:
            by_trade[o.trade_seq].append(o)
    rows: list[dict[str, Any]] = []
    for f in session.scalars(
        select(BacktestFillRecord)
        .where(BacktestFillRecord.run_id == rid)
        .order_by(BacktestFillRecord.seq)
    ):
        o = orders[f.order_seq]
        tseq = f.trade_seq or o.trade_seq
        bar = bars.get((f.asset_id, f.session))
        if bar is None:
            continue
        price, q = Decimal(f.price), Decimal(f.quantity)
        buys = f.side in BUYS
        oh = open_half.get((f.asset_id, f.session), ASSUMED_HALF)
        ch = close_half.get((f.asset_id, f.session), ASSUMED_HALF)
        stop = o.details.get("stop_price")
        limit = o.details.get("limit_price")
        new = price
        cat = f"{f.role}:unchanged"
        inside = bar["l"] <= price <= bar["h"]
        if f.role in ("ENTRY", "TAKE_PROFIT") and limit is not None and price == Decimal(limit):
            lim = Decimal(limit)
            through = bar["o"] < lim if buys else bar["o"] > lim
            # A split session would show the limit far outside the raw bar: not re-priced.
            plausible = abs(bar["o"] - lim) / lim < Decimal("0.25")
            if through and plausible:
                cand = at_open(bar, oh, buys)
                new = min(cand, lim) if buys else max(cand, lim)
                cat = f"{f.role}:A1_opened_through_limit" + ("" if inside else "_never_traded")
            elif not plausible:
                cat = f"{f.role}:split_session_not_repriced"
        elif not inside:
            cat = f"{f.role}:outside_raw_bar"  # e.g. a split-adjusted session: not re-priced
        elif f.role == "STOP" and stop is not None:
            s = Decimal(stop)
            gap = bar["o"] <= s if not buys else bar["o"] >= s
            trade = trades.get(tseq) if tseq else None
            exit_working = trade is not None and any(
                x.role == "EXIT" and x.arrival_at <= f.filled_at and x.status != "FILLED"
                for x in by_trade[tseq]
            )
            if gap:
                new = at_open(bar, oh, buys)
                cat = "STOP:A3_gap"
            elif exit_working:
                new = at_open(bar, oh, buys)
                cat = "STOP:A5_horizon_exit_first"
            else:
                adj = (ch + SLIP) / BPS
                raw = adverse(s * (1 + adj if buys else 1 - adj), buys)
                new = clamp(raw, bar)
                cat = "STOP:A2_no_gap"
        elif f.role == "EXIT":
            new = at_open(bar, oh, buys)
            cat = "EXIT:A4_market_at_open"
        # Positive delta: the corrected price is better for Atlas.
        delta = (price - new) * q if buys else (new - price) * q
        rows.append(
            {
                "category": cat,
                "delta": delta,
                "trade_seq": tseq,
                "strategy": trades[tseq].strategy_id if tseq in trades else None,
                "entry_bar": bool(
                    f.role == "STOP" and tseq in trades and trades[tseq].holding_sessions == 0
                ),
                "notional": price * q,
            }
        )
    closed = [t for t in trades.values() if t.status == "CLOSED"]
    return {"rows": rows, "net": sum(Decimal(t.net_pnl) for t in closed), "trades": len(closed)}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cats: dict[str, dict[str, Any]] = defaultdict(lambda: {"fills": 0, "delta": Decimal(0)})
    for r in rows:
        cats[r["category"]]["fills"] += 1
        cats[r["category"]]["delta"] += r["delta"]
    eb = [r for r in rows if r["entry_bar"]]
    return {
        "by_category": {
            k: {"fills": v["fills"], "delta": round(float(v["delta"]), 2)}
            for k, v in sorted(cats.items())
        },
        "total_delta": round(float(sum(r["delta"] for r in rows)), 2),
        "entry_bar_stop_delta": round(float(sum(r["delta"] for r in eb)), 2),
    }


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    out: dict[str, Any] = {"standalone": {}, "all6": {}}
    pooled: list[dict[str, Any]] = []
    with factory() as session:
        bars = load_bars(session)
        oh = load_spreads(session, "spread-sample-open-v1")
        ch = load_spreads(session, "spread-sample-v1")
        for window in WINDOWS:
            for scope in SCOPES:
                key = f"{window}--live-sip-measured--{scope}"
                res = audit_run(session, run_id(session, manifest, key), bars, oh, ch)
                s = summarise(res["rows"])
                s["baseline_net"] = round(float(res["net"]), 2)
                s["closed_trades"] = res["trades"]
                if scope == "all6":
                    out["all6"][window] = s
                else:
                    out["standalone"][key] = s
                    pooled.extend(res["rows"])
    out["standalone_pooled"] = summarise(pooled)
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in pooled:
        by_strategy[r["strategy"]].append(r)
    out["standalone_by_strategy"] = {k: summarise(v) for k, v in sorted(by_strategy.items())}
    out["standalone_baseline_net"] = round(
        sum(v["baseline_net"] for v in out["standalone"].values()), 2
    )
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(
        json.dumps({k: out[k] for k in ("standalone_pooled", "standalone_baseline_net")}, indent=1)
    )
    for k, v in out["standalone_by_strategy"].items():
        print(k, v["total_delta"], v["entry_bar_stop_delta"])
    for w, v in out["all6"].items():
        print(w, "baseline", v["baseline_net"], "delta", v["total_delta"], v["by_category"])


if __name__ == "__main__":
    main()

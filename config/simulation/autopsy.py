"""Strategy autopsy: read-only breakdowns of stored trades and signals (no backtest).

Two datasets, kept apart:

* **SEEN** (in-sample, examined many times): the engine 8A.2 default runs
  ``y20*--v2-default--<family>`` (2022-01 .. 2026-06; corrected conventions, dividends).
* **STAGE_A** (out-of-sample in time but survivorship-biased; examined once):
  ``{r2020h2,a2021h1,a2021h2}--{e2,e3,control}-primary--<family>``. ``control`` is the
  stored strategy as it is; e2/e3 change only the horizon (and direction for e3).

Per trade, from stored trades/proposals and SIP daily bars (split-adjusted for paths): R,
dollars, costs, dividends, exit reason, holding sessions, regime at entry (known before the
entry), a descriptive sector label (author-assigned current classification, *not* point in
time), the binding risk constraint, dollar risk; maximum favourable / adverse excursion in
ATR while held; the target and stop distance in ATR; the return in ATR 5 and 10 sessions after
the exit; the trade's return against SPY over the same sessions.

Per signal (every proposal the strategies made in the SEEN runs, filled or not): forward
returns 1/5/10/20 sessions in ATR and in excess of SPY.

Statistics use week-clustered standard errors (``diagnose.clustered``). Everything here is
exploratory unless stated otherwise in the report. Writes runs/autopsy.json.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
from analyze import MANIFEST, reg_zone, spy_regimes
from diagnose import clustered, run_id
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    Asset,
    BacktestProposalRecord,
    BacktestTradeRecord,
    MarketBarRecord,
)
from app.database.sessions import build_engine, build_session_factory
from app.learning import regimes as reg

OUT = MANIFEST.parent / "autopsy.json"
FAMILIES = (
    "breakout",
    "event_driven",
    "mean_reversion",
    "momentum",
    "trend_following",
    "volatility",
)
SEEN_WINDOWS = ("y2022", "y2023", "y2024", "y2025", "y2026h1")
STAGE_WINDOWS = ("r2020h2", "a2021h1", "a2021h2")
# Descriptive labels only (current, author-assigned; not point in time, not used by Atlas).
SECTOR = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def bars(session: Any) -> dict[str, tuple[list[date], dict[str, list[float]]]]:
    zone = reg_zone()
    b = MarketBarRecord
    per: dict[str, dict[date, tuple[float, float, float, float]]] = defaultdict(dict)
    for sym, at, o, h, lo, c in session.execute(
        select(Asset.symbol, b.event_time, b.open, b.high, b.low, b.close)
        .join(Asset, Asset.id == b.asset_id)
        .where(b.feed == "sip", b.timeframe == "1Day", b.adjustment == "SPLIT")
        .order_by(Asset.symbol, b.event_time, b.revision)
    ):
        per[sym][at.astimezone(zone).date()] = (float(o), float(h), float(lo), float(c))
    raw: dict[str, dict[date, float]] = defaultdict(dict)
    for sym, at, c in session.execute(
        select(Asset.symbol, b.event_time, b.close)
        .join(Asset, Asset.id == b.asset_id)
        .where(b.feed == "sip", b.timeframe == "1Day", b.adjustment == "RAW")
        .order_by(Asset.symbol, b.event_time, b.revision)
    ):
        raw[sym][at.astimezone(zone).date()] = float(c)
    out = {}
    for sym, by in per.items():
        days = sorted(by)
        out[sym] = (
            days,
            {
                "o": [by[d][0] for d in days],
                "h": [by[d][1] for d in days],
                "l": [by[d][2] for d in days],
                "c": [by[d][3] for d in days],
                "raw": [raw[sym].get(d, math.nan) for d in days],
            },
        )
    return out


def idx(days: list[date], d: date) -> int | None:
    i = bisect_left(days, d)
    return i if i < len(days) and days[i] == d else None


def stat(values: list[float], clusters: list[Any]) -> dict[str, Any]:
    vals = [(v, c) for v, c in zip(values, clusters, strict=True) if not math.isnan(v)]
    if len(vals) < 2:
        return {"n": len(vals)}
    c = clustered([v for v, _ in vals], [k for _, k in vals])
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items()}


def trades_for(session: Any, manifest: dict, key: str, b: dict, regimes: dict) -> list[dict]:
    if manifest.get(key, {}).get("status") != "COMPLETED":
        return []
    rid = run_id(session, manifest, key)
    props = {
        p.seq: p
        for p in session.scalars(
            select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
        )
    }
    spy_days, spy = b["SPY"]
    out = []
    for t in session.scalars(
        select(BacktestTradeRecord).where(
            BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
        )
    ):
        p = props[t.proposal_seq]
        s = 1 if t.direction == "LONG" else -1
        entry, qty = float(t.entry_price), float(t.entry_quantity)
        stop = float(p.invalidation_level)
        risk = abs(entry - stop) * qty
        first = date.fromisoformat(t.details["first_session"])
        last = date.fromisoformat(t.details["last_session"])
        days, ser = b[t.symbol]
        i0, i1, isig = idx(days, first), idx(days, last), idx(days, p.session)
        atr_raw = float((p.details.get("levels") or {}).get("atr") or math.nan)
        row: dict[str, Any] = {
            "family": t.strategy_id,
            "dir": t.direction,
            "symbol": t.symbol,
            "sector": SECTOR.get(t.symbol, "?"),
            "R": float(t.net_pnl) / risk if risk else 0.0,
            "net": float(t.net_pnl),
            "gross": float(t.realized_gross),
            "costs": float(t.costs),
            "dividends": float(t.details.get("dividends", 0) or 0),
            "risk_usd": risk,
            "costs_R": float(t.costs) / risk if risk else math.nan,
            "exit": t.exit_reason,
            "hold": t.holding_sessions or 0,
            "binding": p.binding_constraint or "(none)",
            "strength": float(p.signal_strength),
            "week": tuple(p.session.isocalendar()[:2]),
            "year": p.session.year,
        }
        g = reg.known_before(regimes, t.opened_at.astimezone(reg_zone()).date())
        row["trend"], row["vol"] = g.trend, g.volatility
        if None in (i0, i1, isig) or math.isnan(atr_raw):
            out.append(row)
            continue
        factor = ser["c"][isig] / ser["raw"][isig] if ser["raw"][isig] else math.nan
        atr = atr_raw * factor
        e_adj = entry * (ser["c"][i0] / ser["raw"][i0]) if ser["raw"][i0] else math.nan
        hi = max(ser["h"][i0 : i1 + 1])
        lo = min(ser["l"][i0 : i1 + 1])
        row["mfe_atr"] = ((hi - e_adj) if s > 0 else (e_adj - lo)) / atr
        row["mae_atr"] = ((e_adj - lo) if s > 0 else (hi - e_adj)) / atr
        target = p.target_reference
        row["target_atr"] = abs(float(target) - entry) / atr_raw if target is not None else math.nan
        row["stop_atr"] = abs(entry - stop) / atr_raw
        for k in (5, 10):
            j = i1 + k
            row[f"post{k}_atr"] = (
                s * (ser["c"][j] - ser["c"][i1]) / atr if j < len(ser["c"]) else math.nan
            )
        a, z = idx(spy_days, days[i0 - 1]) if i0 else None, idx(spy_days, last)
        spy_ret = spy["c"][z] / spy["c"][a] - 1 if a is not None and z is not None else math.nan
        ret = float(t.net_pnl) / (entry * qty) if entry * qty else math.nan
        row["ret"], row["spy"], row["excess"] = ret, spy_ret, (ret - s * spy_ret)
        out.append(row)
    return out


def signals_for(session: Any, manifest: dict, key: str, b: dict) -> list[dict]:
    rid = run_id(session, manifest, key)
    spy_days, spy = b["SPY"]
    out = []
    for p in session.scalars(
        select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
    ):
        days, ser = b.get(p.symbol, ([], {}))
        i = idx(days, p.session)
        j = idx(spy_days, p.session)
        atr_raw = float((p.details.get("levels") or {}).get("atr") or math.nan)
        if i is None or j is None or math.isnan(atr_raw) or not ser["raw"][i]:
            continue
        atr = atr_raw * ser["c"][i] / ser["raw"][i]
        s = 1 if p.direction == "LONG" else -1
        row = {
            "family": p.strategy_id,
            "dir": p.direction,
            "week": tuple(p.session.isocalendar()[:2]),
            "filled": p.trade_seq is not None,
        }
        for k in (1, 5, 10, 20):
            if i + k < len(ser["c"]) and j + k < len(spy["c"]):
                r = ser["c"][i + k] / ser["c"][i] - 1
                row[f"f{k}_atr"] = s * (ser["c"][i + k] - ser["c"][i]) / atr
                row[f"x{k}_pct"] = 100 * s * (r - (spy["c"][j + k] / spy["c"][j] - 1))
            else:
                row[f"f{k}_atr"] = row[f"x{k}_pct"] = math.nan
        out.append(row)
    return out


def group(rows: list[dict], key: Any) -> dict[str, Any]:
    gs: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        gs[str(key(r))].append(r)
    return {k: describe(v) for k, v in sorted(gs.items())}


def describe(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    rs = np.asarray([r["R"] for r in rows])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    out: dict[str, Any] = {
        "n": len(rows),
        "net": round(sum(r["net"] for r in rows), 2),
        "R": stat([r["R"] for r in rows], [r["week"] for r in rows]),
        "win_rate": round(float((rs > 0).mean()), 3),
        "avg_win_R": round(float(wins.mean()), 3) if wins.size else None,
        "avg_loss_R": round(float(losses.mean()), 3) if losses.size else None,
        "costs_R_mean": round(float(np.nanmean([r["costs_R"] for r in rows])), 4),
    }
    for f in ("excess", "post5_atr", "post10_atr", "mfe_atr", "mae_atr"):
        vals = [r.get(f, math.nan) for r in rows]
        if any(not math.isnan(v) for v in vals):
            out[f] = stat(vals, [r["week"] for r in rows])
    return out


def distribution(rows: list[dict]) -> dict[str, Any]:
    rs = np.asarray([r["R"] for r in rows])
    net = np.asarray(sorted((r["net"] for r in rows), reverse=True))
    top = max(1, len(net) // 10)
    m, sd = rs.mean(), rs.std(ddof=1)
    return {
        "n": len(rs),
        "quantiles_R_1_5_25_50_75_95_99": [
            round(float(q), 3) for q in np.percentile(rs, [1, 5, 25, 50, 75, 95, 99])
        ],
        "skew": round(float(((rs - m) ** 3).mean() / sd**3), 3),
        "excess_kurtosis": round(float(((rs - m) ** 4).mean() / sd**4 - 3), 3),
        "top10pct_trades_net": round(float(net[:top].sum()), 2),
        "bottom10pct_trades_net": round(float(net[-top:].sum()), 2),
        "total_net": round(float(net.sum()), 2),
        "losses_beyond_minus_1_2R": int((rs < -1.2).sum()),
    }


def geometry(rows: list[dict]) -> dict[str, Any]:
    """Did targets fit the holding period; did stops give back favourable moves."""
    ok = [r for r in rows if "mfe_atr" in r and not math.isnan(r.get("target_atr", math.nan))]
    if not ok:
        return {"n": 0}
    reach = [r for r in ok if r["mfe_atr"] >= r["target_atr"]]
    stops = [r for r in ok if r["exit"] == "STOP_LOSS"]
    return {
        "n": len(ok),
        "median_target_atr": round(float(np.median([r["target_atr"] for r in ok])), 2),
        "median_stop_atr": round(float(np.median([r["stop_atr"] for r in ok])), 2),
        "median_mfe_atr": round(float(np.median([r["mfe_atr"] for r in ok])), 2),
        "median_mae_atr": round(float(np.median([r["mae_atr"] for r in ok])), 2),
        "share_mfe_reached_target": round(len(reach) / len(ok), 3),
        "share_exit_take_profit": round(
            sum(1 for r in ok if r["exit"] == "TAKE_PROFIT") / len(ok), 3
        ),
        "stops_after_mfe_ge_1atr": sum(1 for r in stops if r["mfe_atr"] >= 1),
        "stops": len(stops),
    }


def signal_summary(sig: list[dict]) -> dict[str, Any]:
    out = {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in sig:
        groups[f"{s['family']}:{s['dir']}"].append(s)
    for k, rows in sorted(groups.items()):
        w = [r["week"] for r in rows]
        out[k] = {"n": len(rows)} | {
            f"{m}{h}": stat([r[f"{m}{h}_{u}"] for r in rows], w)
            for m, u in (("f", "atr"), ("x", "pct"))
            for h in (1, 5, 10, 20)
        }
    return out


def section(rows: list[dict]) -> dict[str, Any]:
    return {
        "by_family_direction": group(rows, lambda r: f"{r['family']}:{r['dir']}"),
        "by_direction": group(rows, lambda r: r["dir"]),
        "by_exit": group(rows, lambda r: f"{r['family']}:{r['exit']}"),
        "by_exit_all": group(rows, lambda r: r["exit"]),
        "by_hold": group(
            rows,
            lambda r: (
                "0"
                if r["hold"] == 0
                else "1-2"
                if r["hold"] <= 2
                else "3-5"
                if r["hold"] <= 5
                else "6-10"
                if r["hold"] <= 10
                else "11+"
            ),
        ),
        "by_trend_regime": group(rows, lambda r: f"{r['dir']}:{r['trend']}"),
        "by_vol_regime": group(rows, lambda r: f"{r['dir']}:{r['vol']}"),
        "by_sector": group(rows, lambda r: r["sector"]),
        "by_symbol": group(rows, lambda r: r["symbol"]),
        "by_binding": group(rows, lambda r: r["binding"]),
        "by_risk_quartile": group(rows, lambda r: r["risk_q"]),
        "by_strength_tercile": group(rows, lambda r: r["strength_t"]),
        "by_year": group(rows, lambda r: r["year"]),
        "distribution": {
            f: distribution([r for r in rows if r["family"] == f])
            for f in sorted({r["family"] for r in rows})
        }
        | {"all": distribution(rows)},
        "geometry": {
            f: geometry([r for r in rows if r["family"] == f])
            for f in sorted({r["family"] for r in rows})
        },
        "corr_risk_usd_R": round(
            float(np.corrcoef([r["risk_usd"] for r in rows], [r["R"] for r in rows])[0, 1]), 3
        ),
        "costs": {
            "total_costs": round(sum(r["costs"] for r in rows), 2),
            "total_gross": round(sum(r["gross"] for r in rows), 2),
            "total_dividends": round(sum(r["dividends"] for r in rows), 2),
            "total_net": round(sum(r["net"] for r in rows), 2),
        },
    }


def tag(rows: list[dict]) -> None:
    risks = np.asarray([r["risk_usd"] for r in rows])
    qs = np.percentile(risks, [25, 50, 75])
    for r in rows:
        r["risk_q"] = f"Q{1 + int(np.searchsorted(qs, r['risk_usd']))}"
    for fam in {r["family"] for r in rows}:
        fr = [r for r in rows if r["family"] == fam]
        cuts = np.percentile([r["strength"] for r in fr], [33.3, 66.7])
        for r in fr:
            r["strength_t"] = f"{fam}:T{1 + int(np.searchsorted(cuts, r['strength']))}"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    with factory() as session:
        b = bars(session)
        regimes = spy_regimes(session, "sip")
        seen = []
        sig = []
        for w in SEEN_WINDOWS:
            for f in FAMILIES:
                key = f"{w}--v2-default--{f}"
                seen += trades_for(session, manifest, key, b, regimes)
                sig += signals_for(session, manifest, key, b)
        stage = []
        for w in STAGE_WINDOWS:
            for test, fams in (
                ("control", ("momentum", "breakout", "trend_following")),
                ("e2", ("momentum", "breakout", "trend_following")),
                ("e3", ("momentum",)),
            ):
                for f in fams:
                    for r in trades_for(session, manifest, f"{w}--{test}-primary--{f}", b, regimes):
                        r["family"] = f"{test}:{f}"
                        stage.append(r)
    tag(seen)
    tag(stage)
    result = {
        "SEEN_2022_2026": section(seen),
        "STAGE_A_2020H2_2021": section(stage),
        "signals_SEEN": signal_summary(sig),
        "signals_SEEN_filled_vs_unfilled": {
            k: {
                "filled": stat(
                    [s["f5_atr"] for s in sig if s["dir"] == k and s["filled"]],
                    [s["week"] for s in sig if s["dir"] == k and s["filled"]],
                ),
                "unfilled": stat(
                    [s["f5_atr"] for s in sig if s["dir"] == k and not s["filled"]],
                    [s["week"] for s in sig if s["dir"] == k and not s["filled"]],
                ),
            }
            for k in ("LONG", "SHORT")
        },
    }
    OUT.write_text(json.dumps(result, indent=1, default=str))
    print("seen trades", len(seen), "stage A trades", len(stage), "signals", len(sig))


if __name__ == "__main__":
    main()

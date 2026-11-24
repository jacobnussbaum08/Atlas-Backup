"""Post-simulation strategy diagnosis: signal-level and trade-level evidence (read only).

Reads stored runs of the isolated simulation database (``.env.sim``) and stored SIP daily
bars. Nothing is fitted, no run or parameter is changed. Writes ``runs/diagnostics.json``.

Signals: every proposal of the ``main--8a-sip--<strategy>`` runs (2022-01 → 2026-06, no
coordinator, so every strategy draft that passed the engine's own validation is present,
including those risk later rejected; signals keep being generated after a breaker halt).
Forward returns are measured from the signal close (``reference_entry``) on stored SIP
split-adjusted closes, signed by direction, in percent and in ATR units (the ATR the
strategy itself used). This separates the *signal's* predictive content from the
*execution and exit* structure.

Trades: the standalone ``y20*--live-sip-measured--<strategy>`` runs (fresh account per
calendar year, live pipeline, consolidated data and measured spreads).

Statistics: standard errors are clustered by ISO week of the signal, because signals on
the same day/week across correlated symbols, and repeated signals on consecutive days for
the same symbol, are not independent. The naive i.i.d. t-statistic is shown for contrast.
"""

from __future__ import annotations

import json
import math
import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
from analyze import MANIFEST, reg_zone, spy_regimes
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    Asset,
    BacktestFillRecord,
    BacktestProposalRecord,
    BacktestRunRecord,
    BacktestTradeRecord,
    MarketBarRecord,
)
from app.database.sessions import build_engine, build_session_factory
from app.learning import regimes as reg

OUT = MANIFEST.parent / "diagnostics.json"
STRATEGIES = (
    "breakout",
    "event_driven",
    "mean_reversion",
    "momentum",
    "trend_following",
    "volatility",
)
HORIZONS = (1, 3, 5, 10, 20)
MIN_N = 30


# ----------------------------------------------------------------------------- statistics
def clustered(values: list[float], clusters: list[Any]) -> dict[str, Any]:
    """Mean with an i.i.d. and a cluster-robust standard error (CR0 with small-sample
    factor G/(G-1))."""
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if values else None}
    x = np.asarray(values, dtype=float)
    m = float(x.mean())
    sd = float(x.std(ddof=1))
    iid_t = m / (sd / math.sqrt(n)) if sd > 0 else None
    sums: dict[Any, float] = defaultdict(float)
    for v, c in zip(x - m, clusters, strict=True):
        sums[c] += float(v)
    g = len(sums)
    var = sum(s * s for s in sums.values()) / (n * n) * (g / (g - 1) if g > 1 else 1)
    se = math.sqrt(var) if var > 0 else None
    return {
        "n": n,
        "clusters": g,
        "mean": round(m, 6),
        "t_iid": None if iid_t is None else round(iid_t, 2),
        "t_clustered": None if se is None else round(m / se, 2),
        "hit_rate": round(float((x > 0).mean()), 4),
        "sample": "SUFFICIENT" if n >= MIN_N else "INSUFFICIENT",
    }


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 10:
        return None
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    return round(float(np.corrcoef(ra, rb)[0, 1]), 4)


# ----------------------------------------------------------------------------- data
def run_id(session: Any, manifest: dict, key: str) -> uuid.UUID:
    entry = manifest[key]
    if entry.get("run_id"):
        return uuid.UUID(entry["run_id"])
    rid = session.scalar(
        select(BacktestRunRecord.id)
        .where(BacktestRunRecord.name == f"sim {key}", BacktestRunRecord.status == "COMPLETED")
        .order_by(BacktestRunRecord.created_at.desc())
        .limit(1)
    )
    assert rid is not None, key  # noqa: S101
    return rid


def load_closes(session: Any) -> dict[str, tuple[list[date], list[dict[str, float]]]]:
    """SIP split-adjusted daily bars per symbol, newest revision per session."""
    zone = reg_zone()
    b = MarketBarRecord
    rows = session.execute(
        select(Asset.symbol, b.event_time, b.open, b.high, b.low, b.close, b.revision)
        .join(Asset, Asset.id == b.asset_id)
        .where(b.feed == "sip", b.timeframe == "1Day", b.adjustment == "SPLIT")
        .order_by(Asset.symbol, b.event_time, b.revision)
    ).all()
    per: dict[str, dict[date, dict[str, float]]] = defaultdict(dict)
    for sym, at, o, h, lo, c, _rev in rows:
        per[sym][at.astimezone(zone).date()] = {
            "o": float(o),
            "h": float(h),
            "l": float(lo),
            "c": float(c),
        }
    out = {}
    for sym, by_day in per.items():
        days = sorted(by_day)
        out[sym] = (days, [by_day[d] for d in days])
    return out


# ----------------------------------------------------------------------------- signals
def signal_rows(session: Any, manifest: dict, bars: dict, regimes: dict) -> list[dict]:
    rows = []
    spy_days, spy_series = bars["SPY"]
    spy_close = {d: b["c"] for d, b in zip(spy_days, spy_series, strict=True)}
    for strategy in STRATEGIES:
        rid = run_id(session, manifest, f"main--8a-sip--{strategy}")
        props = session.scalars(
            select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
        )
        for p in props:
            days, series = bars[p.symbol]
            i = bisect_right(days, p.session) - 1
            if i < 0 or days[i] != p.session:
                continue
            c0 = series[i]["c"]
            ref = float(p.reference_entry)
            s = 1 if p.direction == "LONG" else -1
            atr = float(p.details["levels"]["atr"])
            stop_atr = (
                abs(ref - float(p.invalidation_level)) / atr if p.invalidation_level else None
            )
            tgt_atr = abs(float(p.target_reference) - ref) / atr if p.target_reference else None
            # Stored prices are split-adjusted as known at the signal; the series here is
            # adjusted as of today. Measure everything relative to the series close.
            atr_now = atr * c0 / ref
            row: dict[str, Any] = {
                "strategy": strategy,
                "symbol": p.symbol,
                "session": p.session,
                "week": p.session.isocalendar()[:2],
                "direction": p.direction,
                "s": s,
                "strength": float(p.signal_strength),
                "ref_vs_close_bps": (ref / c0 - 1) * 1e4,
                "atr_pct": atr / ref,
                "split_rescaled": abs(ref / c0 - 1) > 0.01,
                "stop_atr": stop_atr,
                "target_atr": tgt_atr,
                "risk": p.risk_result,
                "outcome": p.outcome,
                "regime": reg.known_before(regimes, p.session),
            }
            for k in HORIZONS:
                if i + k < len(series):
                    ck = series[i + k]["c"]
                    row[f"r{k}"] = s * (ck / c0 - 1)
                    row[f"a{k}"] = s * (ck - c0) / atr_now
                    # Excess over SPY on the same sessions (beta assumed 1: a diagnostic
                    # for market drift, not a risk model).
                    spy = spy_close.get(days[i]), spy_close.get(days[i + k])
                    if spy[0] and spy[1]:
                        row[f"x{k}"] = s * ((ck / c0 - 1) - (spy[1] / spy[0] - 1))
            # Counterfactual entry at the next session's open (a market-on-open order):
            # the overnight gap, and the move from that open to later closes.
            if i + 5 < len(series):
                o1 = series[i + 1]["o"]
                row["gap_atr"] = s * (o1 - c0) / atr_now
                row["open5_atr"] = s * (series[i + 5]["c"] - o1) / atr_now
            # Path over the next 5 sessions (the trading horizon), in ATR from the signal
            # close: which of stop / target would a daily-bar check touch first?
            if stop_atr and tgt_atr and i + 6 < len(series):
                first = "NONE"
                mfe = mae = 0.0
                for j in range(1, 7):
                    bar = series[i + j]
                    fav = s * ((bar["h"] if s > 0 else bar["l"]) - c0) / atr_now
                    adv = s * ((bar["l"] if s > 0 else bar["h"]) - c0) / atr_now
                    mfe, mae = max(mfe, fav), min(mae, adv)
                    hit_stop, hit_tgt = adv <= -stop_atr, fav >= tgt_atr
                    if first == "NONE" and (hit_stop or hit_tgt):
                        first = (
                            "BOTH" if hit_stop and hit_tgt else ("STOP" if hit_stop else "TARGET")
                        )
                row["first_touch"] = first
                row["mfe6"], row["mae6"] = mfe, mae
            rows.append(row)
    return rows


def baseline(bars: dict, symbols: set[str], start: date, end: date) -> dict[str, Any]:
    """Unconditional forward returns of every symbol-session in the period (long side)."""
    out: dict[str, Any] = {}
    for k in (5,):
        vals, cl = [], []
        for sym in symbols:
            days, series = bars[sym]
            for i, d in enumerate(days):
                if start <= d <= end and i + k < len(series):
                    vals.append(series[i + k]["c"] / series[i]["c"] - 1)
                    cl.append(d.isocalendar()[:2])
        out[f"r{k}_long_all_symbol_days"] = clustered(vals, cl)
    return out


def summarize_signals(rows: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by = defaultdict(list)
    for r in rows:
        by[r["strategy"]].append(r)
        by[f"{r['strategy']}:{r['direction']}"].append(r)
        by[f"{r['strategy']}:trend_{r['regime'].trend}"].append(r)
        by[f"{r['strategy']}:vol_{r['regime'].volatility}"].append(r)
    for key, rs in sorted(by.items()):
        entry: dict[str, Any] = {"signals": len(rs)}
        for k in HORIZONS:
            sel = [r for r in rs if f"r{k}" in r]
            entry[f"fwd{k}_pct"] = clustered(
                [r[f"r{k}"] * 100 for r in sel], [r["week"] for r in sel]
            )
            entry[f"fwd{k}_atr"] = clustered([r[f"a{k}"] for r in sel], [r["week"] for r in sel])
            sel = [r for r in rs if f"x{k}" in r]
            entry[f"fwd{k}_excess_spy_pct"] = clustered(
                [r[f"x{k}"] * 100 for r in sel], [r["week"] for r in sel]
            )
        sel = [r for r in rs if "open5_atr" in r]
        entry["gap_atr"] = clustered([r["gap_atr"] for r in sel], [r["week"] for r in sel])
        entry["next_open_entry_fwd5_atr"] = clustered(
            [r["open5_atr"] for r in sel], [r["week"] for r in sel]
        )
        touched = [r for r in rs if "first_touch" in r]
        if touched:
            counts: dict[str, int] = defaultdict(int)
            for r in touched:
                counts[r["first_touch"]] += 1
            entry["first_touch_6_sessions"] = {
                k: round(v / len(touched), 4) for k, v in counts.items()
            }
            entry["mean_mfe6_atr"] = round(float(np.mean([r["mfe6"] for r in touched])), 3)
            entry["mean_mae6_atr"] = round(float(np.mean([r["mae6"] for r in touched])), 3)
            entry["stop_atr"] = round(float(np.median([r["stop_atr"] for r in touched])), 2)
            entry["target_atr"] = round(float(np.median([r["target_atr"] for r in touched])), 2)
        if ":" not in key:
            sel = [r for r in rs if "a5" in r]
            entry["strength_vs_fwd5_atr_spearman"] = spearman(
                [r["strength"] for r in sel], [r["a5"] for r in sel]
            )
            if len(sel) >= 3 * MIN_N:
                q = np.quantile([r["strength"] for r in sel], [1 / 3, 2 / 3])
                buckets = {"low": [], "mid": [], "high": []}
                for r in sel:
                    b = (
                        "low"
                        if r["strength"] <= q[0]
                        else ("mid" if r["strength"] <= q[1] else "high")
                    )
                    buckets[b].append(r)
                entry["fwd5_atr_by_strength_tercile"] = {
                    b: clustered([r["a5"] for r in v], [r["week"] for r in v])
                    for b, v in buckets.items()
                }
            fills = defaultdict(list)
            for r in sel:
                fills[r["outcome"] or r["risk"] or "NONE"].append(r)
            entry["fwd5_atr_by_outcome"] = {
                b: clustered([r["a5"] for r in v], [r["week"] for r in v]) for b, v in fills.items()
            }
            # Repeats: the same symbol and direction signalled on the previous session.
            seen = {(r["symbol"], r["direction"], r["session"]) for r in rs}
            entry["share_repeat_signals"] = round(
                sum(
                    1
                    for r in rs
                    if any(
                        (r["symbol"], r["direction"], d) in seen for d in _prev_days(r["session"])
                    )
                )
                / len(rs),
                4,
            )
            by_sym = defaultdict(list)
            for r in sel:
                by_sym[r["symbol"]].append(r)
            entry["fwd5_atr_by_symbol"] = {
                s: clustered([r["a5"] for r in v], [r["week"] for r in v])
                for s, v in sorted(by_sym.items())
            }
        out[key] = entry
    return out


def _prev_days(d: date) -> list[date]:
    from datetime import timedelta

    return [d - timedelta(days=n) for n in range(1, 5)]


def redundancy(rows: list[dict]) -> dict[str, Any]:
    keyset = defaultdict(set)
    daily = defaultdict(lambda: defaultdict(int))
    for r in rows:
        keyset[r["strategy"]].add((r["symbol"], r["session"], r["direction"]))
        daily[r["strategy"]][r["session"]] += r["s"]
    days = sorted({d for s in daily.values() for d in s})
    out: dict[str, Any] = {}
    for i, a in enumerate(STRATEGIES):
        for b in STRATEGIES[i + 1 :]:
            inter = keyset[a] & keyset[b]
            near = {
                k
                for k in keyset[a]
                if any(
                    (k[0], k[1] - __import__("datetime").timedelta(days=n), k[2]) in keyset[b]
                    or (k[0], k[1] + __import__("datetime").timedelta(days=n), k[2]) in keyset[b]
                    for n in range(0, 4)
                )
            }
            va = [daily[a].get(d, 0) for d in days]
            vb = [daily[b].get(d, 0) for d in days]
            corr = float(np.corrcoef(va, vb)[0, 1]) if np.std(va) > 0 and np.std(vb) > 0 else None
            out[f"{a}~{b}"] = {
                "same_symbol_day_direction": len(inter),
                f"share_of_{a}_within_3_days": round(len(near) / max(len(keyset[a]), 1), 4),
                "daily_net_direction_corr": None if corr is None else round(corr, 3),
            }
    return out


# ----------------------------------------------------------------------------- trades
def trade_rows(session: Any, manifest: dict, bars: dict, regimes: dict) -> list[dict]:
    rows = []
    for key, entry in sorted(manifest.items()):
        if not key.startswith("y20") or key.endswith("all6") or entry.get("status") != "COMPLETED":
            continue
        rid = run_id(session, manifest, key)
        props = {
            p.seq: p
            for p in session.scalars(
                select(BacktestProposalRecord).where(BacktestProposalRecord.run_id == rid)
            )
        }
        stop_fills = {
            f.trade_seq: f
            for f in session.scalars(
                select(BacktestFillRecord).where(
                    BacktestFillRecord.run_id == rid, BacktestFillRecord.role == "STOP"
                )
            )
        }
        for t in session.scalars(
            select(BacktestTradeRecord).where(
                BacktestTradeRecord.run_id == rid, BacktestTradeRecord.status == "CLOSED"
            )
        ):
            p = props[t.proposal_seq]
            s = 1 if t.direction == "LONG" else -1
            entry_px = float(t.entry_price)
            stop = float(p.invalidation_level)
            risk_usd = abs(entry_px - stop) * float(t.entry_quantity)
            atr = float(p.details["levels"]["atr"])
            days, series = bars[t.symbol]
            i = bisect_right(days, p.session) - 1
            hold6 = (
                # Return on the current-adjusted series applied to the entry notional.
                s * (series[i + 6]["c"] / series[i]["c"] - 1) * entry_px * float(t.entry_quantity)
                if i + 6 < len(series) and days[i] == p.session
                else None
            )
            sf = stop_fills.get(t.seq)
            rows.append(
                {
                    "strategy": t.strategy_id,
                    "window": key.split("--")[0],
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "week": p.session.isocalendar()[:2],
                    "net": float(t.net_pnl),
                    "gross": float(t.realized_gross) + float(t.unrealized),
                    "costs": float(t.costs),
                    "R": float(t.net_pnl) / risk_usd if risk_usd else None,
                    "risk_usd": risk_usd,
                    "exit": t.exit_reason,
                    "hold": t.holding_sessions,
                    "strength": float(p.signal_strength),
                    "atr_pct": atr / float(p.reference_entry),
                    "hold6_gross": hold6,
                    "stop_gap": None if sf is None else bool(sf.details.get("gap")),
                    "stop_slip_usd": None
                    if sf is None
                    else s * (float(sf.price) - stop) * float(sf.quantity),
                    "regime": reg.known_before(regimes, p.session),
                }
            )
    return rows


def summarize_trades(rows: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by = defaultdict(list)
    for r in rows:
        by[r["strategy"]].append(r)
        by[f"{r['strategy']}:{r['direction']}"].append(r)
    for key, rs in sorted(by.items()):
        wins = [r for r in rs if r["net"] > 0]
        losses = [r for r in rs if r["net"] <= 0]
        exits: dict[str, Any] = {}
        for ex in ("STOP_LOSS", "TAKE_PROFIT", "EXPIRATION"):
            sel = [r for r in rs if r["exit"] == ex]
            if sel:
                exits[ex] = {
                    "share": round(len(sel) / len(rs), 4),
                    "avg_R": round(float(np.mean([r["R"] for r in sel])), 3),
                    "avg_hold": round(float(np.mean([r["hold"] for r in sel])), 2),
                }
        h6 = [r for r in rs if r["hold6_gross"] is not None]
        stops = [r for r in rs if r["stop_gap"] is not None]
        entry = {
            "trades": len(rs),
            "net_R": clustered([r["R"] for r in rs], [r["week"] for r in rs]),
            "net_usd_sum": round(sum(r["net"] for r in rs), 0),
            "gross_usd_sum": round(sum(r["gross"] for r in rs), 0),
            "costs_usd_sum": round(sum(r["costs"] for r in rs), 0),
            "exits": exits,
            "winners": {
                "n": len(wins),
                "avg_R": round(float(np.mean([r["R"] for r in wins])), 3) if wins else None,
                "avg_hold": round(float(np.mean([r["hold"] for r in wins])), 2) if wins else None,
                "avg_strength": round(float(np.mean([r["strength"] for r in wins])), 3)
                if wins
                else None,
                "avg_atr_pct": round(float(np.mean([r["atr_pct"] for r in wins])) * 100, 2)
                if wins
                else None,
            },
            "losers": {
                "n": len(losses),
                "avg_R": round(float(np.mean([r["R"] for r in losses])), 3) if losses else None,
                "avg_hold": round(float(np.mean([r["hold"] for r in losses])), 2)
                if losses
                else None,
                "avg_strength": round(float(np.mean([r["strength"] for r in losses])), 3)
                if losses
                else None,
                "avg_atr_pct": round(float(np.mean([r["atr_pct"] for r in losses])) * 100, 2)
                if losses
                else None,
            },
            # Counterfactual on the same filled trades: hold from the entry price to the
            # close 6 sessions after the signal, no stop or target, before costs.
            "counterfactual_hold6_gross_usd": round(sum(r["hold6_gross"] for r in h6), 0)
            if h6
            else None,
            "actual_gross_usd_same_trades": round(sum(r["gross"] for r in h6), 0) if h6 else None,
            "stop_exits": len(stops),
            "stop_gap_share": round(sum(r["stop_gap"] for r in stops) / len(stops), 4)
            if stops
            else None,
            "stop_slippage_beyond_level_usd": round(sum(r["stop_slip_usd"] for r in stops), 0)
            if stops
            else None,
        }
        if ":" not in key:
            entry["by_symbol"] = {
                s: {
                    "n": len(v),
                    "net_usd": round(sum(r["net"] for r in v), 0),
                    "avg_R": round(float(np.mean([r["R"] for r in v])), 3),
                }
                for s, v in sorted(
                    ((s, [r for r in rs if r["symbol"] == s]) for s in {r["symbol"] for r in rs}),
                    key=lambda kv: sum(r["net"] for r in kv[1]),
                )
            }
            entry["by_window"] = {
                w: {"n": len(v), "avg_R": round(float(np.mean([r["R"] for r in v])), 3)}
                for w, v in sorted(
                    (w, [r for r in rs if r["window"] == w]) for w in {r["window"] for r in rs}
                )
            }
            entry["strength_vs_R_spearman"] = spearman(
                [r["strength"] for r in rs], [r["R"] for r in rs]
            )
        out[key] = entry
    return out


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    with factory() as session:
        regimes = spy_regimes(session, "sip")
        bars = load_closes(session)
        signals = signal_rows(session, manifest, bars, regimes)
        trades = trade_rows(session, manifest, bars, regimes)
    symbols = {r["symbol"] for r in signals}
    result = {
        "signals": summarize_signals(signals),
        "baseline": baseline(bars, symbols, date(2022, 1, 3), date(2026, 6, 26)),
        "redundancy": redundancy(signals),
        "trades": summarize_trades(trades),
        "ref_vs_signal_close_bps_max_abs": round(
            max(abs(r["ref_vs_close_bps"]) for r in signals), 3
        ),
    }
    OUT.write_text(json.dumps(result, indent=1, default=str))
    print(f"wrote {OUT}: {len(signals)} signals, {len(trades)} trades")


if __name__ == "__main__":
    main()

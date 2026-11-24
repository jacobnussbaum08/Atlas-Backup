# ruff: noqa: E501  (result tables read as one row each)
"""X7 (D-256): the historical backtest pipeline on X4.6.1 V2 / X5 data with the X6 engine.

**A hypothetical, long-only historical simulation. No broker, no paper or live trading.**

Flow, per run (a strategy set, an account period and an execution scenario):

1. Inputs: the pinned X4.6.1 V2 and X5 artifacts (``x6_data.load_documents``), the X6
   execution-inputs artifact, the registered strategy versions and the X7 signals artifact,
   every one checked against its SHA-256 before use.
2. Each session *s*, in order: ``begin_session`` (corporate actions, breaks, exits, opening
   executions), ``close_session`` (stops, take-profit targets, close-valued sales, marks,
   checks), then the decisions at the close: horizon exits and entries from the signals
   decided at *s* (computed from data up to *s* only), sized by the registered risk budget and
   reserved atomically by X6 (exits first, then entries by signal strength).
3. Positions: one per slot; the stop (the signal's invalidation) and the take-profit target
   rest from the fill; the stop is checked first when both are reachable in one bar (spec
   18.1); a target fills only when the session trades strictly through it (or opens beyond
   it), never below it. The horizon exit is decided at the close of the H-th session after
   the signal and executes at the next open.
4. Results: equity, trades, costs, rejections, incomplete periods. Headline metrics come only
   from complete equity (``equity_complete``); a run with a frozen position reports its
   complete prefix and the incomplete period separately, never a final value.

Execution scenarios change only execution assumptions (spread, slippage, impact); the signals
are the same in every scenario. ``COSTS_OFF`` is a diagnostic for the cost contribution, not a
result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from app.research import x4_evidence as x4
from app.research import x5_accounting as acc
from app.research import x6_data as xd
from app.research import x6_execution as ex
from app.research import x6_portfolio as xp
from app.research import x7_signals as sg
from app.research import x7_spreads as sp
from app.research.resolution import _plain
from app.research.x6_execution import BPS, ONE, ZERO, Side, X6Config
from app.risk.config import Preferences

D = Decimal
DECISION = "D-256"
OUT = Path(xd.LIFECYCLE)
SIGNALS_PATH = OUT / "x7_signals_v1.json.gz"
RESULTS_PATH = OUT / "x7_backtest_results_v1.json.gz"
X6_ARTIFACT = f"{xd.LIFECYCLE}/x6_execution_inputs_v2.json.gz"
X6_ARTIFACT_SHA256 = "917948267f925a73f443d138f97ff610fb4b9f2bee6f71e8e9b9e6eb8df83c33"
CAPITAL = D("100000")  # spec 14.1 simulated equity
EVALUABLE = ("breakout", "mean_reversion", "momentum", "trend_following", "volatility")
STRATEGY_SETS: dict[str, tuple[str, ...]] = {**{s: (s,) for s in EVALUABLE}, "combined": EVALUABLE}
PERIODS: dict[str, tuple[str, str]] = {  # (formation close the account starts at, last session)
    "continuous_2017_2019": ("2016-12-30", "2019-12-31"),
    "year_2017": ("2016-12-30", "2017-12-29"),
    "year_2018": ("2017-12-29", "2018-12-31"),
    "year_2019": ("2018-12-31", "2019-12-31"),
}
SOURCES = (
    "backend/app/research/x6_data.py", "backend/app/research/x6_execution.py",
    "backend/app/research/x6_portfolio.py", "backend/app/research/x7_signals.py",
    "backend/app/research/x7_pipeline.py",
)  # fmt: skip


# ---------------------------------------------------------------------- scenarios
@dataclass(frozen=True)
class Scenario:
    name: str
    config: X6Config
    spread_multiplier: D
    description: str
    diagnostic: bool = False
    spread_source: str = "MEASURED"  # MEASURED (SIP quotes, fallback 25 bps) or X6_ESTIMATE

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "config_sha256": self.config.sha256(), "spread_multiplier": str(self.spread_multiplier),
                "slippage_bps": str(self.config.preferences.slippage_bps), "impact_coefficient": str(self.config.impact_coefficient),
                "costs_enabled": self.config.costs_enabled, "description": self.description, "diagnostic": self.diagnostic,
                "spread_source": self.spread_source}  # fmt: skip


def _scenario(name: str, desc: str, spread: str = "1", slip: str | None = None, impact: str | None = None,
              costs: bool = True, diagnostic: bool = False, source: str = "MEASURED") -> Scenario:  # fmt: skip
    base = ex.DEFAULT_CONFIG
    prefs = (
        base.preferences
        if slip is None
        else Preferences(**{**base.preferences.model_dump(), "slippage_bps": D(slip)})
    )
    cfg = base.model_copy(update={"preferences": prefs,
                                  "impact_coefficient": base.impact_coefficient if impact is None else D(impact),
                                  "costs_enabled": costs})  # fmt: skip
    return Scenario(
        name, X6Config.model_validate(cfg.model_dump()), D(spread), desc, diagnostic, source
    )


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        _scenario(
            "baseline",
            "measured SIP quoted spread (fallback 25 bps), 5 bps slippage, impact coefficient 1; other X6 defaults",
        ),
        _scenario("spread_x2", "measured (and fallback) spread doubled", spread="2"),
        _scenario("slippage_x2", "slippage 10 bps", slip="10"),
        _scenario("impact_x2", "impact coefficient 2", impact="2"),
        _scenario(
            "all_adverse",
            "spread x2, slippage 10 bps and impact coefficient 2 together",
            spread="2",
            slip="10",
            impact="2",
        ),
        _scenario(
            "x6_estimated_spread",
            "X6's own close-high-low spread estimate instead of the measurements (biased high: see the calibration)",
            source="X6_ESTIMATE",
        ),
        _scenario(
            "costs_off",
            "DIAGNOSTIC ONLY: no spread, slippage, impact, commission or fees (cost contribution)",
            costs=False,
            diagnostic=True,
        ),
    )
}


# ---------------------------------------------------------------------- the portfolio
@dataclass
class X7Portfolio(xp.Portfolio):
    """The X6 portfolio plus resting take-profit targets, trade records and a spread scale."""

    spread_multiplier: D = ONE
    spreads: sp.MeasuredSpreads | None = None  # None: X6's own estimate
    targets: dict[str, D] = field(default_factory=dict)
    intent_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    trade_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    exit_reason: dict[str, str] = field(default_factory=dict)
    skipped: Counter[str] = field(default_factory=Counter)

    def _liquidity(self, slot: str, asof: str) -> ex.Liquidity:
        liq = super()._liquidity(slot, asof)
        if self.spreads is not None:
            found = self.spreads.at(self.market.slots[slot].ticker, asof)
            if found is not None:
                liq = dataclasses.replace(
                    liq, spread_bps=found[0], spread_basis="MEASURED_SIP_QUOTES"
                )
            else:
                liq = dataclasses.replace(
                    liq,
                    spread_bps=self.config.preferences.fallback_spread_bps,
                    spread_basis="ASSUMED_FALLBACK",
                )
        if self.spread_multiplier == ONE:
            return liq
        return dataclasses.replace(liq, spread_bps=liq.spread_bps * self.spread_multiplier)

    def _apply_fill(
        self, fill: ex.Fill, it: xp.OrderIntent, decided: str, extra: dict[str, Any] | None = None
    ) -> None:
        opening = fill.side == Side.BUY and it.slot not in self.positions
        start = len(self.journal.entries)
        self.exit_reason[it.slot] = it.reason  # why a position closed by this fill closed
        super()._apply_fill(fill, it, decided, extra)
        if opening:
            meta = self.intent_meta.get(it.intent_id, {})
            self.trade_meta[it.slot] = {**meta, "entry_session": self.session, "journal_start": start,
                                        "entry_price": str(fill.price), "entry_quantity": str(fill.quantity)}  # fmt: skip
            if meta.get("target") is not None:
                self.targets[it.slot] = D(meta["target"])

    def _close_position(self, pos: xp.Position, why: str) -> None:
        meta = self.trade_meta.pop(pos.slot, {})
        self.targets.pop(pos.slot, None)
        start = meta.get("journal_start", 0)
        cash = rec = charges = ZERO
        for e in self.journal.entries[start:]:
            if e.get("slot") != pos.slot:
                continue
            lines = {k: D(v) for k, v in e["lines"].items()}
            cash += lines.get("CASH", ZERO)
            rec += lines.get("RECEIVABLE_DIVIDENDS", ZERO) + lines.get("RECEIVABLE_EXIT_CASH", ZERO)
            charges += sum(
                (
                    lines.get(k, ZERO)
                    for k in ("COMMISSION_EXPENSE", "FEE_EXPENSE", "REGULATORY_FEE_EXPENSE")
                ),
                ZERO,
            )
        pnl = cash + rec
        if why in (
            "SELL",
            "BUY_TO_COVER",
        ):  # a fill: the order's purpose (stop, target, horizon, window end)
            why = self.exit_reason.get(pos.slot, why)
        self.trades.append({"slot": pos.slot, "strategy_id": meta.get("strategy_id"), "signal_session": meta.get("signal_session"),
                            "entry_session": meta.get("entry_session"), "exit_session": self.session, "exit": why,
                            "pnl": str(pnl), "charges": str(charges), "receivables": str(rec),
                            "entry_price": meta.get("entry_price"), "entry_quantity": meta.get("entry_quantity")})  # fmt: skip
        super()._close_position(pos, why)

    def _stops(self, s: str) -> None:
        stopped_before = {
            f["slot"] for f in self.fills if f.get("session") == s and f.get("reason") == "STOP"
        }
        super()._stops(s)
        stopped = {
            f["slot"] for f in self.fills if f.get("session") == s and f.get("reason") == "STOP"
        } | stopped_before
        for slot in sorted(self.targets):
            pos = self.positions.get(slot)
            sd = self._slot(slot)
            if (
                pos is None
                or pos.state != "OPEN"
                or pos.quantity <= 0
                or slot in stopped
                or not sd.executable(s)
            ):
                continue
            target, bar = self.targets[slot], sd.bars[s]
            if bar.open >= target:
                reference = bar.open
            elif bar.high > target:  # touching is not filling
                reference = target
            else:
                continue
            prev = self.snapshots[-1]["session"]
            fill = target_fill(
                pos.quantity, bar, reference, target, self._liquidity(slot, prev), self.config
            )
            if fill.quantity > 0:
                self._apply_fill(fill, xp.OrderIntent(f"target:{slot}:{s}", slot, Side.SELL, pos.quantity, reason="TARGET"),
                                 decided=prev, extra={"target": str(target), "gap": reference == bar.open})  # fmt: skip


def target_fill(
    quantity: D, bar: xd.Bar, reference: D, target: D, liq: ex.Liquidity, cfg: X6Config
) -> ex.Fill:
    """A resting sell limit at ``target``: from the reference (the open when the session opened
    beyond it, else the target) less the execution costs, never below the target and inside
    the session's range; capped by the session's volume share."""
    qty = min(quantity.to_integral_value(ROUND_FLOOR), ex.session_cap(bar, cfg))
    bps = ex.cost_bps(qty, liq, cfg)
    raw = reference * (ONE - bps["total"] / BPS)
    tick = ex.tick_for(raw)
    price = max((raw / tick).to_integral_value(ROUND_FLOOR) * tick, target)
    price = min(max(price, bar.low), bar.high)
    reasons = ("CAPPED_BY_SESSION_VOLUME",) if qty < quantity else ()
    notional = qty * price
    return ex.Fill(Side.SELL, bar.session, cfg.timing, quantity, qty, reference, price, False, bps,
                   ex.commission(qty, cfg), ex.other_fees(notional, cfg), ex.regulatory_fee(notional, Side.SELL, bar.session, cfg),
                   reasons)  # fmt: skip


# ---------------------------------------------------------------------- one run
def _index(sessions: list[str]) -> dict[str, int]:
    return {s: i for i, s in enumerate(sessions)}


def simulate(market: xd.Market, signals: list[dict[str, Any]], strategies: tuple[str, ...], period: tuple[str, str],
             scenario: Scenario, spreads: sp.MeasuredSpreads | None = None) -> X7Portfolio:  # fmt: skip
    """One deterministic run. Signals decided at a session's close become entry intents
    executing at the next session; nothing later than the decision session is read."""
    start, end = period
    sessions = xd._sessions(ex.next_session(start), end)
    allsess = [start, *sessions]
    idx = _index(allsess)
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sig in signals:
        if sig["strategy_id"] in strategies and start <= sig["session"] < end:
            by_session[sig["session"]].append(sig)
    pf = X7Portfolio(
        market,
        scenario.config,
        CAPITAL,
        start,
        spread_multiplier=scenario.spread_multiplier,
        spreads=spreads if scenario.spread_source == "MEASURED" else None,
    )
    h = scenario.config.hard_limits
    for s in sessions:
        pf.begin_session(s)
        pf.close_session()
        intents: list[xp.OrderIntent] = []
        for slot in sorted(pf.positions):
            pos, meta = pf.positions[slot], pf.trade_meta.get(slot)
            if (
                pos.state != "OPEN"
                or pos.quantity <= 0
                or not meta
                or meta.get("signal_session") not in idx
            ):
                continue
            if idx[s] - idx[meta["signal_session"]] >= int(meta["horizon"]):
                intents.append(
                    xp.OrderIntent(
                        f"{s}|horizon|{slot}", slot, Side.SELL, pos.quantity, reason="HORIZON"
                    )
                )
        busy = set(pf.positions) | {r.intent.slot for r in pf.reservations}
        chosen: dict[str, dict[str, Any]] = {}
        nxt = ex.next_session(s)
        for sig in sorted(
            by_session.get(s, []), key=lambda x: (-D(x["strength"]), x["strategy_id"], x["slot"])
        ):
            window_end = market.slots[sig["slot"]].window[1]
            if nxt == window_end and window_end != xd.DATA_END:
                # an entry at the window's last session could not be exited inside the window
                pf.skipped["ENTRY_ON_WINDOW_LAST_SESSION"] += 1
                continue
            if sig["slot"] not in busy and sig["slot"] not in chosen:
                chosen[sig["slot"]] = sig
        equity = pf.equity()
        ranked = sorted(chosen.values(), key=lambda x: (-D(x["strength"]), x["slot"]))
        for rank, sig in enumerate(ranked):
            entry, stop = D(sig["entry"]), D(sig["stop"]) if sig["stop"] is not None else None
            if stop is None or stop >= entry:
                continue
            qty = (h.risk_per_trade_pct * equity / (entry - stop)).to_integral_value(ROUND_FLOOR)
            if qty <= 0:
                continue
            iid = f"{s}|entry|{rank:04d}|{sig['strategy_id']}|{sig['slot']}"
            pf.intent_meta[iid] = {"strategy_id": sig["strategy_id"], "signal_session": s, "horizon": sig["horizon"],
                                   "target": sig["target"], "strength": sig["strength"]}  # fmt: skip
            intents.append(
                xp.OrderIntent(
                    iid, sig["slot"], Side.BUY, qty, stop_price=stop, reason=sig["strategy_id"]
                )
            )
        pf.decide(intents)
    return pf


# ---------------------------------------------------------------------- metrics
def _f(x: Any) -> float:
    return float(x)


def series_metrics(equity: list[tuple[str, D]]) -> dict[str, Any]:
    """Return metrics of one complete daily equity series (first value = starting equity)."""
    if len(equity) < 2:
        return {"sessions": len(equity), "note": "too short"}
    values = [_f(v) for _, v in equity]
    rets = [b / a - 1 for a, b in itertools.pairwise(values)]
    n = len(rets)
    total = values[-1] / values[0] - 1
    peak, mdd, mdd_at = values[0], 0.0, equity[0][0]
    for (s, _), v in zip(equity, values, strict=True):
        peak = max(peak, v)
        if peak and (v / peak - 1) < mdd:
            mdd, mdd_at = v / peak - 1, s
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets) if n > 1 else 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n)
    return {
        "start": equity[0][0], "end": equity[-1][0], "sessions": n,
        "start_equity": str(equity[0][1]), "end_equity": str(equity[-1][1]),
        "total_return": round(total, 6),
        "annualized_return": round((1 + total) ** (252 / n) - 1, 6),
        "max_drawdown": round(mdd, 6), "max_drawdown_session": mdd_at,
        "annualized_volatility": round(sd * math.sqrt(252), 6),
        "sharpe": None if sd == 0 else round(mean / sd * math.sqrt(252), 4),
        "sortino": None if downside == 0 else round(mean / downside * math.sqrt(252), 4),
    }  # fmt: skip


def trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [D(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    return {
        "closed_trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": None if not pnls else round(len(wins) / len(pnls), 4),
        "average_win": None if not wins else str((gross_win / len(wins)).quantize(D("0.01"))),
        "average_loss": None if not losses else str((-gross_loss / len(losses)).quantize(D("0.01"))),
        "profit_factor": None if gross_loss == 0 else round(_f(gross_win / gross_loss), 4),
        "net_pnl": str(sum(pnls, ZERO).quantize(D("0.01"))),
        "by_exit": dict(sorted(Counter(t["exit"] for t in trades).items())),
        "by_strategy": {k: {"trades": len(v), "net_pnl": str(sum((D(t["pnl"]) for t in v), ZERO).quantize(D("0.01")))}
                        for k, v in sorted(_group(trades, "strategy_id").items())},
    }  # fmt: skip


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[str(r.get(key))].append(r)
    return out


def cost_breakdown(pf: X7Portfolio) -> dict[str, Any]:
    b = pf.journal.balance
    explicit = {
        k: str(b(k))
        for k in ("COMMISSION_EXPENSE", "FEE_EXPENSE", "REGULATORY_FEE_EXPENSE", "BORROW_EXPENSE")
    }
    implicit = ZERO
    for f in pf.fills:
        if f.get("status") in ("FILLED", "PARTIAL"):
            q, price, ref = D(f["quantity"]), D(f["price"]), D(f["reference"])
            implicit += (
                (price - ref) * q if f["side"] in ("BUY", "BUY_TO_COVER") else (ref - price) * q
            )
    total = (
        sum(
            (
                b(k)
                for k in (
                    "COMMISSION_EXPENSE",
                    "FEE_EXPENSE",
                    "REGULATORY_FEE_EXPENSE",
                    "BORROW_EXPENSE",
                )
            ),
            ZERO,
        )
        + implicit
    )
    return {"explicit": explicit, "implicit_vs_reference": str(implicit.quantize(D("0.01"))),
            "implicit_note": "fill price minus the reference (open, or the stop/target reference) times quantity: half spread, slippage and impact actually paid; sales of distributed or received shares (17.5 bps assumed) are not included",
            "total": str(total.quantize(D("0.01"))), "total_pct_of_capital": round(_f(total / CAPITAL), 6)}  # fmt: skip


def exposure_metrics(snaps: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    eq = [D(s["equity"]) for s in snaps]
    lm = [D(s["long_market_value"]) for s in snaps]
    sp = [D(s["spendable"]) for s in snaps]
    rc = [sum((D(v) for v in s["receivables"].values()), ZERO) for s in snaps]
    traded = sum(
        (
            D(f["quantity"]) * D(f["price"])
            for f in fills
            if f.get("status") in ("FILLED", "PARTIAL")
        ),
        ZERO,
    )
    mean_eq = sum(eq, ZERO) / len(eq)
    years = len(snaps) / 252
    return {
        "average_exposure": round(statistics.fmean(_f(a / e) for a, e in zip(lm, eq, strict=True)), 4),
        "time_in_market": round(sum(1 for s in snaps if s["positions"]) / len(snaps), 4),
        "average_idle_spendable_cash": round(statistics.fmean(_f(a / e) for a, e in zip(sp, eq, strict=True)), 4),
        "average_receivables": round(statistics.fmean(_f(a / e) for a, e in zip(rc, eq, strict=True)), 6),
        "traded_notional": str(traded.quantize(D("0.01"))),
        "annual_turnover": round(_f(traded / mean_eq) / years, 4) if years else None,
        "entries_blocked_sessions": dict(sorted(Counter(b for s in snaps for b in s["entries_blocked"]).items())),
    }  # fmt: skip


def complete_prefix(
    snaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The snapshots before the first incomplete one, and the incomplete period's description."""
    for i, s in enumerate(snaps):
        if not s["equity_complete"]:
            tail = snaps[i:]
            return snaps[:i], {
                "first_incomplete_session": s["session"], "last_session": tail[-1]["session"], "sessions": len(tail),
                "frozen_positions": sorted({f for t in tail for f in t["frozen"]}),
                "max_frozen_share_of_equity_at_last_mark": None,
            }  # fmt: skip
    return snaps, None


# ---------------------------------------------------------------------- regimes and benchmarks
def spy_regimes(market: xd.Market) -> dict[str, dict[str, str]]:
    """Point-in-time market regimes at each session's close, from SPY's own bars up to then:
    TREND (close above / below its 200-session average) and VOLATILITY (20-session realized
    volatility above / below its trailing 252-session median). UNKNOWN without the history."""
    sd = market.slots[sg.BENCHMARK_SLOT]
    dates = sorted(d for d in sd.bars if sd.executable(d))
    closes = [float(sd.bars[d].close) for d in dates]
    vols: list[float | None] = []
    out: dict[str, dict[str, str]] = {}
    for i, d in enumerate(dates):
        trend = "UNKNOWN"
        if i >= 199:
            sma = statistics.fmean(closes[i - 199 : i + 1])
            trend = "UP" if closes[i] > sma else "DOWN"
        vol = None
        if i >= 20:
            r = [math.log(closes[j] / closes[j - 1]) for j in range(i - 19, i + 1)]
            vol = statistics.stdev(r) * math.sqrt(252)
        vols.append(vol)
        hist = [v for v in vols[-252:] if v is not None]
        level = "UNKNOWN"
        if vol is not None and len(hist) >= 126:
            level = "HIGH" if vol > statistics.median(hist) else "LOW"
        out[d] = {"trend": trend, "volatility": level}
    return out


def regime_returns(
    equity: list[tuple[str, D]], regimes: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Daily returns grouped by the regime known at the previous close."""
    groups: dict[str, list[float]] = defaultdict(list)
    for (a, ea), (_, eb) in itertools.pairwise(equity):
        r = _f(eb / ea) - 1
        reg = regimes.get(a, {"trend": "UNKNOWN", "volatility": "UNKNOWN"})
        groups[f"trend_{reg['trend']}"].append(r)
        groups[f"volatility_{reg['volatility']}"].append(r)
    return {k: {"sessions": len(v), "mean_daily_return": round(statistics.fmean(v), 7),
                "compounded_return": round(math.prod(1 + x for x in v) - 1, 6)} for k, v in sorted(groups.items())}  # fmt: skip


def benchmark_equity(
    series: dict[str, Any],
    start: str,
    end: str,
    sd: xd.SlotData | None = None,
    market: xd.Market | None = None,
) -> list[tuple[str, D]]:
    """A buy-and-hold total-return path (X5 factors, dividends reinvested) from ``start``'s close.
    When ``start`` is the slot's formation, the step to the first session is the X6 formation
    step (no X5 series contains it)."""
    value = CAPITAL
    out = [(start, value)]
    if sd is not None and start == sd.formation:
        step = xd.formation_step(sd, market)
        if step["status"] != "COMPUTED":
            raise ValueError(f"{sd.slot}: no formation step ({step['status']})")
        value = value * D(step["gross"])
        out.append((sd.window[0], value))
    for d, g in series["returns"]:
        if start < d <= end:
            value = value * xd.dec(g)
            out.append((d, value))
    return out


def equal_weight_universe(
    market: xd.Market, series: dict[str, dict[str, Any]], start: str, end: str
) -> dict[str, Any]:
    """Daily-rebalanced equal weight of the stock members effective that session, from X5
    factors: membership from the formation (point in time); a member without a factor that day
    (a break, an unvalued exit, the end of its data) is left out of that day, not bridged; a
    valued exit's terminal step counts on its valuation session; a window's first session counts
    by the X6 formation step (no X5 series contains it)."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for slot, ser in series.items():
        sd = market.slots[slot]
        if sd.pool != "STOCK":
            continue
        if start < sd.window[0] <= end:
            step = xd.formation_step(sd, market)
            if step["status"] == "COMPUTED":
                by_day[sd.window[0]].append(float(step["gross"]))
        for d, g in ser["returns"]:
            if start < d <= end and sd.member_effective(d):
                by_day[d].append(g)
        t = ser.get("terminal_step")
        if t and t["session"] and start < t["session"] <= end and sd.member_effective(t["from"]):
            by_day[t["session"]].append(t["gross"])
    value = CAPITAL
    path = [(start, value)]
    for d in xd._sessions(ex.next_session(start), end):
        if by_day.get(d):
            value = value * D(repr(statistics.fmean(by_day[d])))
        path.append((d, value))
    return {"metrics": series_metrics(path), "member_days": sum(len(v) for v in by_day.values())}


# ---------------------------------------------------------------------- reconciliation
def reconcile(pf: X7Portfolio) -> dict[str, Any]:
    """An independent rebuild of cash and receivables from the fill and event records (not
    the journal), compared with the journal's balances."""
    cash = CAPITAL
    for f in pf.fills:
        if f.get("status") not in ("FILLED", "PARTIAL"):
            continue
        n = D(f["quantity"]) * D(f["price"])
        charges = D(f["commission"]) + D(f["fees"]) + D(f["regulatory_fee"])
        cash += (n - charges) if f["side"] == "SELL" else (-n - charges)
    rec = ZERO
    for e in pf.events:
        if e["event"] == "DIVIDEND_RECEIVABLE":
            rec += D(e["amount"])
        elif e["event"] == "DISTRIBUTION_SOLD":
            cash += D(e["proceeds"])
        elif e["event"] == "EXIT":
            cash += D(e["proceeds"])
            rec += D(e["receivable"])
    lieu = sum((D(r["amount"]) for r in pf.receivables if r["kind"] == "CASH_IN_LIEU"), ZERO)
    rec += lieu
    journal_rec = pf.receivables_total()
    return {
        "cash_from_records": str(cash), "cash_journal": str(pf.cash), "cash_equal": cash == pf.cash,
        "receivables_from_records": str(rec), "receivables_journal": str(journal_rec),
        "receivables_equal": rec == journal_rec, "cash_in_lieu": str(lieu),
        "journal_balanced": sum(pf.journal.balances.values(), ZERO) == 0,
        "invariant_violations": pf.violations,
    }  # fmt: skip


# ---------------------------------------------------------------------- one run's record
def run_record(market: xd.Market, docs: dict[str, dict[str, Any]], signals: list[dict[str, Any]], set_name: str,
               period_name: str, scenario: Scenario, regimes: dict[str, dict[str, str]],
               spreads: sp.MeasuredSpreads | None = None) -> dict[str, Any]:  # fmt: skip
    period = PERIODS[period_name]
    pf = simulate(market, signals, STRATEGY_SETS[set_name], period, scenario, spreads)
    snaps = pf.snapshots
    prefix, incomplete = complete_prefix(snaps)
    equity = [(s["session"], D(s["equity"])) for s in prefix]
    if incomplete is not None:
        last = snaps[-1]
        frozen_value = sum(
            (p.market_value for p in pf.positions.values() if p.state == "FROZEN"), ZERO
        )
        incomplete["max_frozen_share_of_equity_at_last_mark"] = round(
            _f(frozen_value / D(last["equity"])), 6
        )
        incomplete["frozen_detail"] = [{"slot": e["slot"], "session": e["session"], "reason": e["reason"],
                                        "mark": e["mark"], "mark_session": e["mark_session"]}
                                       for e in pf.events if e["event"] == "FROZEN"]  # fmt: skip
    tr = docs[xd.X5_TOTAL_RETURNS]["series"]
    complete = incomplete is None
    end = equity[-1][0] if equity else period[0]
    headline = series_metrics(equity) if equity else {"note": "no complete equity"}
    rejected = Counter(r for d in pf.decisions if d["status"] == "REJECTED" for r in d["reasons"])
    resized = Counter(r for d in pf.decisions if d["status"] == "RESIZED" for r in d["reasons"])
    no_fill = Counter(r for f in pf.fills if f.get("status") == "NO_FILL" for r in f["reasons"])
    by_year: dict[str, Any] = {}
    for y in ("2017", "2018", "2019"):
        pts = [(s, v) for s, v in equity if s[:4] == y]
        prev = [(s, v) for s, v in equity if s[:4] < y]
        if pts and prev:
            by_year[y] = {"return": round(_f(pts[-1][1] / prev[-1][1]) - 1, 6), "through": pts[-1][0],
                          "complete_year": pts[-1][0] == max(d for d in xd._sessions(f"{y}-01-01", f"{y}-12-31"))}  # fmt: skip
    spy = benchmark_equity(
        tr[sg.BENCHMARK_SLOT], period[0], end, market.slots[sg.BENCHMARK_SLOT], market
    )
    return {
        "strategy_set": set_name, "period": period_name, "scenario": scenario.name, "diagnostic": scenario.diagnostic,
        "account": {"start": period[0], "end": period[1], "capital": str(CAPITAL)},
        "equity_complete": complete,
        "headline_basis": "full period" if complete else f"COMPLETE PREFIX ONLY through {end}; the rest is incomplete (frozen positions) and is reported separately",
        "headline": headline,
        "incomplete": incomplete,
        "trades": trade_metrics(pf.trades),
        "open_positions_at_end": [p.as_json() for p in sorted(pf.positions.values(), key=lambda p: p.slot)],
        "costs": cost_breakdown(pf),
        "exposure": exposure_metrics(prefix or snaps[:1], pf.fills),
        "by_year": by_year,
        "by_regime": regime_returns(equity, regimes),
        "benchmarks": {"SPY_total_return": series_metrics(spy),
                       "equal_weight_stock_members": equal_weight_universe(market, tr, period[0], end)},
        "orders": {"decisions": len(pf.decisions), "approved": sum(1 for d in pf.decisions if d["status"] == "APPROVED"),
                   "resized": sum(1 for d in pf.decisions if d["status"] == "RESIZED"),
                   "rejected": sum(1 for d in pf.decisions if d["status"] == "REJECTED"),
                   "rejected_by_reason": dict(sorted(rejected.items())), "resized_by_reason": dict(sorted(resized.items())),
                   "fills": sum(1 for f in pf.fills if f.get("status") == "FILLED"),
                   "partial_fills": sum(1 for f in pf.fills if f.get("status") == "PARTIAL"),
                   "no_fill_by_reason": dict(sorted(no_fill.items())),
                   "signals_not_entered": dict(sorted(pf.skipped.items()))},
        "reconciliation": reconcile(pf),
        "journal_sha256": hashlib.sha256(x4.canonical(pf.journal.entries)).hexdigest(),
        "equity_curve": [[s["session"], s["equity"], s["equity_complete"]] for s in snaps],
        "trade_list": pf.trades,
    }  # fmt: skip


# ---------------------------------------------------------------------- artifacts
def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_inputs(signals_sha: str | None = None) -> dict[str, Any]:
    """Every pin, checked before a build or a run; raises on any difference."""
    pins = dict(xd.verify_pins())
    x6 = _sha(X6_ARTIFACT)
    if x6 != X6_ARTIFACT_SHA256:
        raise xd.PinError(f"{X6_ARTIFACT}: not the pinned X6 artifact")
    pins[X6_ARTIFACT] = {"expected": X6_ARTIFACT_SHA256, "found": x6}
    strategies = _sha(sg.STRATEGIES_PATH)
    if strategies != sg.STRATEGIES_SHA256:
        raise xd.PinError(f"{sg.STRATEGIES_PATH}: not the pinned capture")
    pins[sg.STRATEGIES_PATH] = {"expected": sg.STRATEGIES_SHA256, "found": strategies}
    spreads = _sha(sp.PATH)
    if spreads != sp.SHA256:
        raise xd.PinError(f"{sp.PATH}: not the pinned spread measurements")
    pins[sp.PATH.as_posix()] = {"expected": sp.SHA256, "found": spreads}
    if signals_sha is not None:
        found = _sha(SIGNALS_PATH)
        if found != signals_sha:
            raise xd.PinError(f"{SIGNALS_PATH}: not the signals the results were built from")
        pins[SIGNALS_PATH.as_posix()] = {"expected": signals_sha, "found": found}
    return pins


def content_sha256(doc: dict[str, Any]) -> str:
    return acc.content_sha256(doc)


def write_once(doc: dict[str, Any], path: Path) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: X7 artifacts are written once")
    data = x4.gz(x4.canonical(_plain(doc)))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def signal_pins() -> dict[str, Any]:
    """What signals depend on: the X4.6.1 V2 and X5 artifacts and the registered strategies."""
    pins = dict(xd.verify_pins())
    found = _sha(sg.STRATEGIES_PATH)
    if found != sg.STRATEGIES_SHA256:
        raise xd.PinError(f"{sg.STRATEGIES_PATH}: not the pinned capture")
    pins[sg.STRATEGIES_PATH] = {"expected": sg.STRATEGIES_SHA256, "found": found}
    return pins


def build_signals_doc(workers: int = 6) -> dict[str, Any]:
    pins = signal_pins()
    docs = xd.load_documents()
    market = xd.Market.from_documents(docs)
    built = sg.build_signals(market, workers)
    regs = sg.load_registered()
    doc: dict[str, Any] = {
        "kind": "x7_signals", "decision": DECISION, "code": "X7_SIGNALS_V1",
        "inputs": {"pins": pins, "sources_sha256": {p: _sha(p) for p in SOURCES if p.endswith(("x6_data.py", "x7_signals.py"))}},
        "strategies": {k: {"version": r.version["version"], "logic_version": r.version["logic_version"],
                           "parameters_sha256": r.version["parameters_sha256"], "evaluated": k not in sg.NOT_EVALUABLE,
                           "not_evaluated_reason": sg.NOT_EVALUABLE.get(k)} for k, r in sorted(regs.items())},
        "rules": {"direction": "LONG only: no asset is reported shortable (no borrow evidence)",
                  "decision_sessions": "the slot is executable at the session and a member at the next session",
                  "inputs": "executable bars up to the decision session, restated for splits and share factors with an ex-date on or before it; SPY (pinned fund member) as the benchmark; Phase 3B regimes and Phase 3C news UNAVAILABLE"},
        "summary": {"signals": len(built["signals"]),
                    "by_strategy": dict(sorted(Counter(s["strategy_id"] for s in built["signals"]).items())),
                    "evaluation_states": built["states"]},
        "signals": built["signals"],
        "per_slot_states": built["per_slot"],
    }  # fmt: skip
    doc["content_sha256"] = content_sha256(doc)
    return doc


def build_results(
    signals_sha: str, runs: list[tuple[str, str, str]] | None = None
) -> dict[str, Any]:
    pins = verify_inputs(signals_sha)
    docs = xd.load_documents()
    market = xd.Market.from_documents(docs)
    sig_doc = x4.read_json(SIGNALS_PATH)
    if sig_doc["content_sha256"] != content_sha256(sig_doc):
        raise xd.PinError("signals artifact content hash differs")
    regimes = spy_regimes(market)
    spreads = sp.MeasuredSpreads.load(sp.window_for(ex.DEFAULT_CONFIG.timing))
    plan = runs or [(s, p, c) for s in STRATEGY_SETS for p in PERIODS for c in SCENARIOS]
    records = [
        run_record(market, docs, sig_doc["signals"], s, p, SCENARIOS[c], regimes, spreads)
        for s, p, c in plan
    ]
    doc: dict[str, Any] = {
        "kind": "x7_backtest_results", "decision": DECISION, "code": "X7_BACKTEST_RESULTS_V1",
        "label": "HYPOTHETICAL LONG-ONLY HISTORICAL SIMULATION (2017-2019, survivorship-free X4 universe). Not brokerage activity; no short selling evaluated; registered strategy parameters, untuned.",
        "inputs": {"pins": pins, "signals_content_sha256": sig_doc["content_sha256"], "sources_sha256": {p: _sha(p) for p in SOURCES}},
        "scenarios": {k: v.as_json() for k, v in SCENARIOS.items()},
        "assumptions": ex.DEFAULT_CONFIG.assumptions(),
        "spread_input": {"source": "measured historical SIP quoted spreads (x7_spreads), the open window for opening executions; 25 bps assumed where none is known within 45 days",
                         "calibration_of_x6_estimate": sp.calibration(market)},
        "signal_summary": sig_doc["summary"],
        "strategies_not_evaluated": sg.NOT_EVALUABLE,
        "methodology": METHODOLOGY,
        "runs": records,
    }  # fmt: skip
    doc["content_sha256"] = content_sha256(doc)
    return doc


METHODOLOGY: dict[str, str] = {
    "equity": "Equity at each session's close: cash + restricted short proceeds + receivables + positions at their marks (X6). Receivables (dividends, exit cash, cash in lieu) are counted in equity and never spendable.",
    "complete_equity": "Headline metrics use only snapshots with equity_complete true. A run with a frozen position (a restricted spin-off, a missing session, an unvalued exit or a window end without data) reports its complete prefix as the headline basis, labelled, and the incomplete period separately; no frozen position is liquidated or valued at a later price.",
    "returns": "Daily simple returns of complete equity. Total return = last / first - 1. Annualized = (1 + total)^(252 / sessions) - 1.",
    "drawdown": "Maximum peak-to-trough decline of complete daily equity.",
    "sharpe": "Mean daily return / standard deviation (n - 1) x sqrt(252), with a zero risk-free rate: no risk-free series is in the pinned evidence, so this is not an excess-return Sharpe.",
    "sortino": "Mean daily return / downside deviation x sqrt(252); downside deviation = sqrt(mean(min(r, 0)^2)) over all sessions; target 0.",
    "trades": "A trade runs from the first buy into a flat slot to the position's close (sell, stop, target, horizon, window end or exit conversion). P&L = the slot's cash and receivable movements in the journal over the trade: net of commission, fees and execution costs, dividends included. Open or frozen positions at the end are not trades. Trade statistics cover every closed trade of the run, including trades closed after the first incomplete session (each is fully realized); exit reasons: STOP, TARGET, HORIZON, WINDOW_END, EXIT (a valued corporate exit).",
    "profit_factor": "Sum of winning trades' P&L / absolute sum of losing trades' P&L (a zero-P&L trade counts as a loss).",
    "turnover": "Traded notional / mean equity, per 252 sessions.",
    "exposure": "Mean long market value / equity; time in market = share of sessions with a position.",
    "costs": "Explicit: commission, fees, regulatory fees (none configured), borrow (none). Implicit: fill price versus its reference (the session open, or a stop/target reference) times quantity. The costs_off diagnostic scenario gives the gross path; its difference from baseline also reflects changed sizing and fills.",
    "regimes": "From SPY's own bars up to the previous close: TREND = close above / below its 200-session mean; VOLATILITY = 20-session realized volatility above / below its trailing 252-session median (UNKNOWN without the history). Not the Phase 3B regimes, which are not stored for 2017-2019.",
    "benchmarks": "SPY total return: X5 daily factors of the pinned SPY fund member (dividends reinvested at the ex-date close), buy and hold from the account's start; from the 2016-12-30 formation close, the step to 2017-01-03 is X6's formation step. Each stock window's first session enters the equal-weight index by its members' X6 formation steps. Equal-weight stock members: daily-rebalanced mean of X5 factors of stock members effective that session (membership known at the formation); a member without a factor that day is left out, not bridged; valued exits enter by their terminal step.",
    "sizing": "Quantity = floor(risk_per_trade_pct (0.5%) x equity at the decision / (signal entry - signal stop)); X6 then applies every hard limit atomically (positions, single asset, gross, net, class, cash, ADV participation, spread, per-trade risk with costs, breakers) and may resize or reject.",
    "selection": "No entry executes on a slot's last window session (its automatic window-end exit is decided the session before), except at the end of the data (2019-12-31). One position per slot. A session's signals are ranked by signal strength (descending), then slot; in the combined set, a slot signalled by several strategies takes the strongest. Exits are reserved before entries.",
}


# ---------------------------------------------------------------------- commands
def cli_signals(workers: int = 6) -> dict[str, Any]:
    doc = build_signals_doc(workers)
    sha = write_once(doc, SIGNALS_PATH)
    return {
        "signals": SIGNALS_PATH.as_posix(),
        "file_sha256": sha,
        "content_sha256": doc["content_sha256"],
        "summary": doc["summary"],
    }


def cli_backtest() -> dict[str, Any]:
    doc = build_results(_sha(SIGNALS_PATH))
    sha = write_once(doc, RESULTS_PATH)
    return {
        "results": RESULTS_PATH.as_posix(),
        "file_sha256": sha,
        "content_sha256": doc["content_sha256"],
        "runs": len(doc["runs"]),
    }


def verify(workers: int = 6) -> dict[str, Any]:
    """Every pin; the signals and the results rebuilt from scratch and compared by content."""
    stored_s, stored_r = x4.read_json(SIGNALS_PATH), x4.read_json(RESULTS_PATH)
    signals = build_signals_doc(workers)
    results = build_results(_sha(SIGNALS_PATH))
    checks = {
        "signals": [
            stored_s["content_sha256"],
            content_sha256(stored_s),
            signals["content_sha256"],
        ],
        "results": [
            stored_r["content_sha256"],
            content_sha256(stored_r),
            results["content_sha256"],
        ],
    }
    recon = [r["reconciliation"] for r in results["runs"]]
    ok_recon = all(
        r["cash_equal"]
        and r["receivables_equal"]
        and r["journal_balanced"]
        and not r["invariant_violations"]
        for r in recon
    )
    return {**checks, "files": {SIGNALS_PATH.as_posix(): _sha(SIGNALS_PATH), RESULTS_PATH.as_posix(): _sha(RESULTS_PATH)},
            "reconciliation_ok": ok_recon,
            "ok": all(len(set(v)) == 1 for v in checks.values()) and ok_recon}  # fmt: skip

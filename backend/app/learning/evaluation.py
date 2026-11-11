"""Evaluating a replay over a window of sessions, and segmenting it (Phase 8B).

Everything is computed from a :class:`~app.backtesting.engine.BacktestResult` (the 8A
replay's own records); nothing is estimated.

* **Daily returns** of a window: session-to-session changes of the replay's net equity for
  the sessions in the window; the first uses the equity at the previous session's valuation
  (or the initial capital when the window starts the replay). Gross returns add the
  cumulative costs back. Risk-free rate zero.
* **Trades** of a window are purged (:func:`app.learning.windows.purge_trades`): opened in
  it and closed by its end. Net P/L per trade is after its own commissions, fees and borrow.
* **Sample status.** Values are always computed, and flagged ``INSUFFICIENT_SAMPLE`` below
  :data:`MIN_RETURNS` daily returns (risk ratios) or :data:`MIN_TRADES` closed trades (trade
  statistics). An insufficient sample is never promoted to a conclusion.
* **Benchmark** return: the benchmark's RAW close at the session before the window to its
  close at the window's last session (price return; no dividends, as for the portfolio).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.backtesting.data import HistoricalDataset
from app.backtesting.engine import BacktestResult, Trade
from app.backtesting.metrics import holding_sessions
from app.learning import statistics as st
from app.learning.enums import Finding, ResultStatus
from app.learning.regimes import UNKNOWN, Regime
from app.learning.windows import purge_trades

MIN_RETURNS = 60
MIN_TRADES = 30
MIN_SEGMENT_TRADES = 10
ANNUAL = math.sqrt(252)


@dataclass(frozen=True)
class TradeStats:
    count: int
    wins: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    net_pnl: float
    gross_pnl: float
    costs: float
    average_holding_sessions: float | None
    largest_share_of_profit: float | None  # top 5 winners / total net profit (if positive)
    long_trades: int
    short_trades: int
    status: ResultStatus

    def as_json(self) -> dict[str, Any]:
        return {
            "trades": self.count,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "net_pnl": self.net_pnl,
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "average_holding_sessions": self.average_holding_sessions,
            "top5_share_of_net_profit": self.largest_share_of_profit,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "status": self.status.value,
        }


def trade_stats(trades: Sequence[Trade], minimum: int = MIN_TRADES) -> TradeStats:
    pnls = [float(t.net_pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    total = sum(pnls)
    holds = [h for t in trades if (h := holding_sessions(t)) is not None]
    top = sorted(wins, reverse=True)[:5]
    return TradeStats(
        count=n,
        wins=len(wins),
        win_rate=len(wins) / n if n else None,
        average_win=sum(wins) / len(wins) if wins else None,
        average_loss=sum(losses) / len(losses) if losses else None,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        expectancy=total / n if n else None,
        net_pnl=total,
        gross_pnl=total + sum(float(t.costs) for t in trades),
        costs=sum(float(t.costs) for t in trades),
        average_holding_sessions=sum(holds) / len(holds) if holds else None,
        largest_share_of_profit=sum(top) / total if total > 0 else None,
        long_trades=sum(1 for t in trades if t.long),
        short_trades=sum(1 for t in trades if not t.long),
        status=ResultStatus.OK if n >= minimum else ResultStatus.INSUFFICIENT_SAMPLE,
    )


def trade_stats_from_json(m: dict[str, Any], status: ResultStatus) -> TradeStats:
    return TradeStats(
        count=m["trades"],
        wins=m["wins"],
        win_rate=m["win_rate"],
        average_win=m["average_win"],
        average_loss=m["average_loss"],
        profit_factor=m["profit_factor"],
        expectancy=m["expectancy"],
        net_pnl=m["net_pnl"],
        gross_pnl=m["gross_pnl"],
        costs=m["costs"],
        average_holding_sessions=m["average_holding_sessions"],
        largest_share_of_profit=m["top5_share_of_net_profit"],
        long_trades=m["long_trades"],
        short_trades=m["short_trades"],
        status=status,
    )


@dataclass
class WindowEvaluation:
    first: date
    last: date
    sessions: list[date]
    daily_returns: list[float]
    gross_returns: list[float]
    start_equity: float
    end_equity: float
    net_return: float
    gross_return: float
    max_drawdown: float
    sharpe: float | None  # annualized
    sortino: float | None
    costs: float
    turnover: float | None
    trades: TradeStats
    kept_trades: list[Trade] = field(repr=False, default_factory=list)
    purge: dict[str, int] = field(default_factory=dict)
    benchmark_return: float | None = None
    open_positions_at_end: int = 0

    @property
    def returns_status(self) -> ResultStatus:
        return (
            ResultStatus.OK
            if len(self.daily_returns) >= MIN_RETURNS
            else ResultStatus.INSUFFICIENT_SAMPLE
        )

    @property
    def excess_return(self) -> float | None:
        return None if self.benchmark_return is None else self.net_return - self.benchmark_return

    def as_json(self) -> dict[str, Any]:
        return {
            "first": self.first.isoformat(),
            "last": self.last.isoformat(),
            "sessions": len(self.sessions),
            "start_equity": self.start_equity,
            "end_equity": self.end_equity,
            "net_return": self.net_return,
            "gross_return": self.gross_return,
            "max_drawdown": self.max_drawdown,
            "sharpe_annualized": self.sharpe,
            "sortino_annualized": self.sortino,
            "returns_status": self.returns_status.value,
            "costs": self.costs,
            "turnover": self.turnover,
            "benchmark_return": self.benchmark_return,
            "excess_return": self.excess_return,
            "open_positions_at_end": self.open_positions_at_end,
            "purged_trades": self.purge,
            "trade_statistics": self.trades.as_json(),
        }


def _sortino(r: Sequence[float]) -> float | None:
    if len(r) < 2:
        return None
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in r) / len(r))
    if downside == 0:
        return None
    return sum(r) / len(r) / downside * ANNUAL


def _compound(r: Iterable[float]) -> float:
    out = 1.0
    for x in r:
        out *= 1 + x
    return out - 1


def _drawdown(equity: Sequence[float], start: float) -> float:
    peak, worst = start, 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def benchmark_return(
    dataset: HistoricalDataset, benchmark: Any, first: date, last: date
) -> float | None:
    if benchmark is None:
        return None
    bars = dataset.raw.get(benchmark, {})
    before = [d for d, b in bars.items() if d < first and b.valid]
    end = bars.get(last)
    if not before or end is None or not end.valid:
        return None
    start = bars[max(before)].close
    return float(end.close / start - 1) if start > 0 else None


def evaluate_window(
    result: BacktestResult,
    capital: Decimal,
    first: date,
    last: date,
    *,
    dataset: HistoricalDataset | None = None,
    benchmark: Any = None,
    trade_filter: Callable[[Trade], bool] | None = None,
    min_trades: int = MIN_TRADES,
) -> WindowEvaluation:
    snaps = [s for s in result.snapshots if first <= s.session <= last]
    earlier = [s for s in result.snapshots if s.session < first]
    prev_equity = float(earlier[-1].equity) if earlier else float(capital)
    prev_costs = float(earlier[-1].total_costs) if earlier else 0.0
    start_equity, start_costs = prev_equity, prev_costs
    net: list[float] = []
    gross: list[float] = []
    equity_path: list[float] = []
    for s in snaps:
        e = float(s.equity)
        c = float(s.total_costs)
        net.append(e / prev_equity - 1 if prev_equity > 0 else 0.0)
        g_prev = prev_equity + prev_costs
        gross.append((e + c) / g_prev - 1 if g_prev > 0 else 0.0)
        prev_equity, prev_costs = e, c
        equity_path.append(e)
    end_equity = equity_path[-1] if equity_path else start_equity
    end_costs = float(snaps[-1].total_costs) if snaps else start_costs
    trades = [t for t in result.trades if trade_filter is None or trade_filter(t)]
    purge = purge_trades(trades, first, last)
    notional = sum(float(f.quantity * f.price) for f in result.fills if first <= f.session <= last)
    average_equity = sum(equity_path) / len(equity_path) if equity_path else 0.0
    sr = st.sharpe(net)
    return WindowEvaluation(
        first=first,
        last=last,
        sessions=[s.session for s in snaps],
        daily_returns=net,
        gross_returns=gross,
        start_equity=start_equity,
        end_equity=end_equity,
        net_return=_compound(net),
        gross_return=_compound(gross),
        max_drawdown=_drawdown(equity_path, start_equity),
        sharpe=None if sr is None else sr * ANNUAL,
        sortino=_sortino(net),
        costs=end_costs - start_costs,
        turnover=notional / average_equity if average_equity > 0 else None,
        trades=trade_stats(purge.kept, min_trades),
        kept_trades=purge.kept,
        purge={
            "opened_before_window": purge.before_window,
            "open_at_window_end": purge.open_at_end,
        },
        benchmark_return=None
        if dataset is None
        else benchmark_return(dataset, benchmark, first, last),
        open_positions_at_end=snaps[-1].open_positions if snaps else 0,
    )


# ---------------------------------------------------------------------- objective
OBJECTIVES = ("NET_SHARPE", "NET_EXPECTANCY", "NET_RETURN")


def objective(ev: WindowEvaluation, kind: str, min_trades: int) -> float | None:
    """Selection score of a window (higher is better). A window with fewer than
    ``min_trades`` closed trades scores None: it cannot be selected on so little evidence."""
    if ev.trades.count < min_trades:
        return None
    if kind == "NET_SHARPE":
        sr = st.sharpe(ev.daily_returns)
        return None if sr is None else sr
    if kind == "NET_EXPECTANCY":
        return ev.trades.expectancy
    if kind == "NET_RETURN":
        return ev.net_return
    raise ValueError(f"unknown objective {kind}")


# ---------------------------------------------------------------------- segments
def segment_trades(
    trades: Sequence[Trade],
    key: Callable[[Trade], str],
    minimum: int = MIN_SEGMENT_TRADES,
) -> dict[str, TradeStats]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k: trade_stats(v, minimum) for k, v in sorted(groups.items())}


def regime_of_trade(
    regimes: dict[date, Regime], ordered_days: list[date]
) -> Callable[[Trade], Regime]:
    import bisect

    def lookup(t: Trade) -> Regime:
        i = bisect.bisect_left(ordered_days, t.first_session)
        return regimes[ordered_days[i - 1]] if i > 0 else UNKNOWN

    return lookup


def returns_by_regime(
    sessions: Sequence[date],
    returns: Sequence[float],
    regimes: dict[date, Regime],
    attribute: str,
) -> dict[str, dict[str, Any]]:
    """Daily returns grouped by the regime known at the previous session's close."""
    import bisect

    days = sorted(regimes)
    groups: dict[str, list[float]] = defaultdict(list)
    for day, r in zip(sessions, returns, strict=True):
        i = bisect.bisect_left(days, day)
        regime = regimes[days[i - 1]] if i > 0 else UNKNOWN
        groups[getattr(regime, attribute)].append(r)
    out: dict[str, dict[str, Any]] = {}
    for label, rs in sorted(groups.items()):
        sr = st.sharpe(rs)
        out[label] = {
            "sessions": len(rs),
            "mean_daily_return": sum(rs) / len(rs),
            "compound_return": _compound(rs),
            "sharpe_annualized": None if sr is None else sr * ANNUAL,
            "status": (
                ResultStatus.OK if len(rs) >= MIN_RETURNS else ResultStatus.INSUFFICIENT_SAMPLE
            ).value,
        }
    return out


# ---------------------------------------------------------------------- findings
@dataclass(frozen=True)
class Thresholds:
    min_oos_trades: int = MIN_TRADES
    min_oos_sessions: int = MIN_RETURNS
    max_drawdown: float = 0.15
    max_top5_share: float = 0.5
    degradation_ratio: float = 0.5  # OOS score below this share of IS: degradation
    min_dsr: float = 0.95
    max_pbo: float = 0.5
    min_profit_factor: float = 1.2  # spec 20.1 proposed gate
    min_positive_fold_share: float = 0.6

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def findings(
    *,
    oos: WindowEvaluation,
    fold_net_returns: Sequence[float],
    in_sample_sharpe: float | None,
    oos_sharpe: float | None,
    regime_stats: dict[str, TradeStats],
    dsr: float | None,
    pbo: float | None,
    cost_stress_net: float | None,
    neighbor_positive_share: float | None,
    thresholds: Thresholds,
) -> tuple[list[Finding], list[str]]:
    """Classify the out-of-sample evidence. Returns (findings, reasons)."""
    f: list[Finding] = []
    why: list[str] = []
    t = oos.trades
    sufficient = (
        t.count >= thresholds.min_oos_trades
        and len(oos.daily_returns) >= thresholds.min_oos_sessions
    )
    if not sufficient:
        f.append(Finding.INSUFFICIENT_EVIDENCE)
        why.append(
            f"out of sample: {t.count} closed trades (need {thresholds.min_oos_trades}) over "
            f"{len(oos.daily_returns)} sessions (need {thresholds.min_oos_sessions})"
        )
    if t.count and t.net_pnl <= 0:
        if t.gross_pnl > 0:
            f.append(Finding.UNPROFITABLE_AFTER_COSTS)
            why.append(f"gross trade P/L {t.gross_pnl:.2f} becomes {t.net_pnl:.2f} after costs")
        else:
            f.append(Finding.UNPROFITABLE)
            why.append(f"net trade P/L {t.net_pnl:.2f} out of sample")
    if oos.max_drawdown > thresholds.max_drawdown:
        f.append(Finding.EXCESSIVE_DRAWDOWN)
        why.append(f"out-of-sample drawdown {oos.max_drawdown:.1%} > {thresholds.max_drawdown:.0%}")
    if (
        t.largest_share_of_profit is not None
        and t.largest_share_of_profit > thresholds.max_top5_share
    ):
        f.append(Finding.DEPENDS_ON_FEW_TRADES)
        why.append(f"the 5 best trades are {t.largest_share_of_profit:.0%} of the net profit")
    if (
        in_sample_sharpe is not None
        and in_sample_sharpe > 0
        and (oos_sharpe is None or oos_sharpe < thresholds.degradation_ratio * in_sample_sharpe)
    ):
        f.append(Finding.DEGRADES_OUT_OF_SAMPLE)
        why.append(
            f"in-sample Sharpe {in_sample_sharpe:.2f} vs out-of-sample "
            f"{'n/a' if oos_sharpe is None else f'{oos_sharpe:.2f}'}"
        )
    usable = {
        k: v for k, v in regime_stats.items() if k != "UNKNOWN" and v.status is ResultStatus.OK
    }
    signs = {k: v.net_pnl > 0 for k, v in usable.items()}
    if len(signs) >= 2 and len(set(signs.values())) == 2:
        f.append(Finding.REGIME_DEPENDENT)
        why.append(
            "profitable only in "
            + ", ".join(k for k, s in signs.items() if s)
            + "; losing in "
            + ", ".join(k for k, s in signs.items() if not s)
        )
    if pbo is not None and pbo > thresholds.max_pbo:
        f.append(Finding.LIKELY_OVERFIT)
        why.append(f"probability of backtest overfitting {pbo:.2f} > {thresholds.max_pbo}")
    if cost_stress_net is not None and t.net_pnl > 0 and cost_stress_net <= 0:
        f.append(Finding.COST_SENSITIVE)
        why.append("out-of-sample profit disappears at twice the assumed costs")
    if neighbor_positive_share is not None and neighbor_positive_share < 0.5:
        f.append(Finding.PARAMETER_UNSTABLE)
        why.append(
            f"only {neighbor_positive_share:.0%} of neighbouring parameter sets were profitable"
        )
    positive_folds = [r > 0 for r in fold_net_returns]
    consistent = (
        sufficient
        and positive_folds
        and sum(positive_folds) / len(positive_folds) >= thresholds.min_positive_fold_share
        and t.net_pnl > 0
        and (t.profit_factor or 0) >= thresholds.min_profit_factor
        and dsr is not None
        and dsr >= thresholds.min_dsr
        and not any(
            x in f
            for x in (
                Finding.EXCESSIVE_DRAWDOWN,
                Finding.DEPENDS_ON_FEW_TRADES,
                Finding.LIKELY_OVERFIT,
                Finding.DEGRADES_OUT_OF_SAMPLE,
            )
        )
    )
    if consistent:
        f.append(Finding.CONSISTENT)
        why.append("positive in most folds, sufficient trades, deflated Sharpe above threshold")
    return f, why


__all__ = [
    "MIN_RETURNS",
    "MIN_SEGMENT_TRADES",
    "MIN_TRADES",
    "OBJECTIVES",
    "Thresholds",
    "TradeStats",
    "WindowEvaluation",
    "benchmark_return",
    "evaluate_window",
    "findings",
    "objective",
    "regime_of_trade",
    "returns_by_regime",
    "segment_trades",
    "trade_stats",
]

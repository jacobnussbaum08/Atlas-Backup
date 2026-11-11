"""Performance metrics and benchmark comparisons of a backtest (Phase 8A).

Definitions (all from the run's own records; nothing is estimated):

* **Net** figures are after every simulated cost (commissions, fees, short borrow); **gross**
  figures add the run's cumulative costs back (``gross equity = equity + costs to date``).
* Total return = final equity / initial capital - 1. Net P/L = final equity - capital.
* Daily returns are session-to-session changes of equity at each session's valuation
  (the first from the initial capital). Risk-free rate: zero (stated, not estimated).
* Annualized return = (1 + total)^(252 / sessions) - 1, only for runs of at least
  :data:`MIN_SESSIONS_ANNUAL` sessions; shorter runs report NOT_APPLICABLE, because
  annualizing a few weeks multiplies noise.
* Volatility, Sharpe and Sortino (annualized with sqrt(252)) need at least
  :data:`MIN_RETURNS_RISK` daily returns; otherwise INSUFFICIENT_SAMPLE with no value.
  Sortino uses the downside deviation of negative returns (target 0); with no negative
  return it is NOT_APPLICABLE.
* Maximum drawdown: the largest fall of equity from a running peak (the initial capital
  counts as the first peak), as an amount and a fraction of that peak.
* Trade statistics use **closed** trades (net P/L after their own costs). With fewer than
  :data:`MIN_TRADES` closed trades the values are shown and flagged INSUFFICIENT_SAMPLE.
  Win rate = winners / closed; average winner/loser = mean net P/L of each group; profit
  factor = sum of winners / |sum of losers| (NOT_APPLICABLE without a loser); expectancy =
  mean net P/L per closed trade; holding period in sessions from the entry fill's session
  to the exit's session.
* Turnover = sum of fill notionals / average equity over the run.
* Benchmarks are price returns (no dividends: the portfolio receives none either) bought at
  the first session's open and valued at each session's close, so they cover exactly the
  run's dates. The buy-and-hold reference is an equal-weight basket of the run's universe at
  the start.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.asset_discovery.sessions import trading_day
from app.backtesting.data import CALENDAR, HistoricalDataset, sessions_between
from app.backtesting.engine import BacktestResult, Snapshot, Trade
from app.backtesting.enums import BenchmarkKind, MetricScope, MetricStatus, TradeStatus

ZERO = Decimal(0)
SESSIONS_PER_YEAR = 252
MIN_SESSIONS_ANNUAL = 252
MIN_RETURNS_RISK = 126
MIN_TRADES = 30
RATIO = Decimal("0.00000001")
MONEY = Decimal("0.01")


@dataclass
class Metric:
    scope: MetricScope
    scope_key: str
    metric: str
    value: Decimal | None
    unit: str
    status: MetricStatus
    sample_size: int
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "metric": self.metric,
            "value": None if self.value is None else str(self.value),
            "unit": self.unit,
            "status": self.status.value,
            "sample_size": self.sample_size,
            "detail": self.detail,
        }


def _d(value: float | Decimal | None, places: Decimal = RATIO) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        value = Decimal(repr(value))
    return value.quantize(places)


def daily_returns(
    snapshots: Sequence[Snapshot], capital: Decimal, *, gross: bool = False
) -> list[float]:
    out: list[float] = []
    previous = capital
    for s in snapshots:
        current = s.equity + (s.total_costs if gross else ZERO)
        if previous > 0:
            out.append(float(current / previous - 1))
        previous = current
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def max_drawdown(values: Iterable[Decimal], start: Decimal) -> tuple[Decimal, Decimal]:
    """(largest fall from a running peak, as an amount; as a fraction of that peak)."""
    peak, worst, worst_pct = start, ZERO, ZERO
    for v in values:
        peak = max(peak, v)
        fall = peak - v
        if fall > worst:
            worst = fall
        if peak > 0 and fall / peak > worst_pct:
            worst_pct = fall / peak
    return worst, worst_pct


def risk_metrics(
    returns: Sequence[float], scope: MetricScope, key: str, prefix: str = ""
) -> list[Metric]:
    n = len(returns)
    out: list[Metric] = []
    if n < MIN_RETURNS_RISK:
        why = f"{n} daily returns; at least {MIN_RETURNS_RISK} are required"
        for name in ("volatility_annualized", "sharpe_ratio", "sortino_ratio"):
            out.append(
                Metric(
                    scope,
                    key,
                    prefix + name,
                    None,
                    "ratio",
                    MetricStatus.INSUFFICIENT_SAMPLE,
                    n,
                    why,
                )
            )
        return out
    sd = _stdev(returns)
    mean = _mean(returns)
    annual = math.sqrt(SESSIONS_PER_YEAR)
    out.append(
        Metric(
            scope,
            key,
            prefix + "volatility_annualized",
            _d(sd * annual),
            "ratio",
            MetricStatus.OK,
            n,
            "sample standard deviation x sqrt(252)",
        )
    )
    if sd == 0:
        out.append(
            Metric(
                scope,
                key,
                prefix + "sharpe_ratio",
                None,
                "ratio",
                MetricStatus.NOT_APPLICABLE,
                n,
                "zero volatility",
            )
        )
    else:
        out.append(
            Metric(
                scope,
                key,
                prefix + "sharpe_ratio",
                _d(mean / sd * annual),
                "ratio",
                MetricStatus.OK,
                n,
                "mean / sd of daily returns x sqrt(252); rf = 0",
            )
        )
    downside = [min(r, 0.0) for r in returns]
    if not any(r < 0 for r in returns):
        out.append(
            Metric(
                scope,
                key,
                prefix + "sortino_ratio",
                None,
                "ratio",
                MetricStatus.NOT_APPLICABLE,
                n,
                "no negative daily return",
            )
        )
    else:
        dd = math.sqrt(sum(r * r for r in downside) / n)
        out.append(
            Metric(
                scope,
                key,
                prefix + "sortino_ratio",
                _d(mean / dd * annual),
                "ratio",
                MetricStatus.OK,
                n,
                "mean / downside deviation (target 0) x sqrt(252)",
            )
        )
    return out


def holding_sessions(trade: Trade) -> int | None:
    if trade.last_session is None:
        return None
    return max(len(sessions_between(trade.first_session, trade.last_session)) - 1, 0)


def trade_metrics(trades: Sequence[Trade], scope: MetricScope, key: str) -> list[Metric]:
    closed = [t for t in trades if t.status is TradeStatus.CLOSED]
    n = len(closed)
    status = MetricStatus.OK if n >= MIN_TRADES else MetricStatus.INSUFFICIENT_SAMPLE
    flag = "" if n >= MIN_TRADES else f"{n} closed trades; fewer than {MIN_TRADES}"
    out = [
        Metric(scope, key, "trades_closed", Decimal(n), "count", MetricStatus.OK, n),
        Metric(
            scope,
            key,
            "trades_open_at_end",
            Decimal(sum(1 for t in trades if t.status is TradeStatus.OPEN_AT_END)),
            "count",
            MetricStatus.OK,
            len(trades),
        ),
        Metric(
            scope,
            key,
            "net_pnl_trades",
            sum((t.net_pnl for t in trades), ZERO).quantize(MONEY),
            "USD",
            MetricStatus.OK,
            len(trades),
            "closed and open trades, after their costs",
        ),
        Metric(
            scope,
            key,
            "trade_costs",
            sum((t.costs for t in trades), ZERO).quantize(MONEY),
            "USD",
            MetricStatus.OK,
            len(trades),
            "commissions, fees and borrow of the trades",
        ),
    ]
    if n == 0:
        for name in (
            "win_rate",
            "average_winner",
            "average_loser",
            "profit_factor",
            "expectancy",
            "average_holding_sessions",
        ):
            out.append(
                Metric(
                    scope, key, name, None, "", MetricStatus.NOT_APPLICABLE, 0, "no closed trade"
                )
            )
        return out
    pnl = [t.net_pnl for t in closed]
    winners = [p for p in pnl if p > 0]
    losers = [p for p in pnl if p <= 0]
    out.append(
        Metric(
            scope,
            key,
            "win_rate",
            _d(Decimal(len(winners)) / n),
            "ratio",
            status,
            n,
            flag or "net P/L > 0",
        )
    )
    out.append(
        Metric(
            scope,
            key,
            "average_winner",
            (sum(winners, ZERO) / len(winners)).quantize(MONEY) if winners else None,
            "USD",
            status if winners else MetricStatus.NOT_APPLICABLE,
            len(winners),
            flag if winners else "no winning trade",
        )
    )
    out.append(
        Metric(
            scope,
            key,
            "average_loser",
            (sum(losers, ZERO) / len(losers)).quantize(MONEY) if losers else None,
            "USD",
            status if losers else MetricStatus.NOT_APPLICABLE,
            len(losers),
            flag if losers else "no losing trade",
        )
    )
    gross_loss = abs(sum(losers, ZERO))
    out.append(
        Metric(
            scope,
            key,
            "profit_factor",
            _d(sum(winners, ZERO) / gross_loss) if gross_loss > 0 else None,
            "ratio",
            status if gross_loss > 0 else MetricStatus.NOT_APPLICABLE,
            n,
            flag if gross_loss > 0 else "no losing trade",
        )
    )
    out.append(
        Metric(
            scope,
            key,
            "expectancy",
            (sum(pnl, ZERO) / n).quantize(MONEY),
            "USD",
            status,
            n,
            flag or "mean net P/L per closed trade",
        )
    )
    held = [h for t in closed if (h := holding_sessions(t)) is not None]
    out.append(
        Metric(
            scope,
            key,
            "average_holding_sessions",
            _d(Decimal(sum(held)) / len(held), Decimal("0.01")) if held else None,
            "sessions",
            status if held else MetricStatus.NOT_APPLICABLE,
            len(held),
            flag,
        )
    )
    return out


def portfolio_metrics(result: BacktestResult, capital: Decimal) -> list[Metric]:
    snaps = result.snapshots
    n = len(snaps)
    final = snaps[-1] if snaps else None
    P = MetricScope.PORTFOLIO
    out: list[Metric] = []
    if final is None:
        return out
    costs = final.total_costs
    net = final.equity - capital
    total = final.equity / capital - 1
    gross_total = (final.equity + costs) / capital - 1
    out += [
        Metric(P, "portfolio", "sessions", Decimal(n), "count", MetricStatus.OK, n),
        Metric(P, "portfolio", "initial_capital", capital, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "final_equity", final.equity, "USD", MetricStatus.OK, n),
        Metric(
            P,
            "portfolio",
            "net_pnl",
            net.quantize(MONEY),
            "USD",
            MetricStatus.OK,
            n,
            "after all simulated costs",
        ),
        Metric(
            P,
            "portfolio",
            "gross_pnl",
            (net + costs).quantize(MONEY),
            "USD",
            MetricStatus.OK,
            n,
            "before commissions, fees and borrow",
        ),
        Metric(P, "portfolio", "total_return", _d(total), "ratio", MetricStatus.OK, n, "net"),
        Metric(
            P,
            "portfolio",
            "gross_total_return",
            _d(gross_total),
            "ratio",
            MetricStatus.OK,
            n,
            "before costs",
        ),
        Metric(P, "portfolio", "realized_pnl", final.realized_pnl, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "unrealized_pnl", final.unrealized_pnl, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "commissions", final.commissions, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "fees", final.fees, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "borrow_costs", final.borrow_costs, "USD", MetricStatus.OK, n),
        Metric(P, "portfolio", "total_costs", costs, "USD", MetricStatus.OK, n),
        Metric(
            P, "portfolio", "costs_pct_of_capital", _d(costs / capital), "ratio", MetricStatus.OK, n
        ),
    ]
    if n >= MIN_SESSIONS_ANNUAL and total > -1:
        annual = (1 + float(total)) ** (SESSIONS_PER_YEAR / n) - 1
        out.append(
            Metric(
                P,
                "portfolio",
                "annualized_return",
                _d(annual),
                "ratio",
                MetricStatus.OK,
                n,
                "(1 + total)^(252/sessions) - 1",
            )
        )
    else:
        out.append(
            Metric(
                P,
                "portfolio",
                "annualized_return",
                None,
                "ratio",
                MetricStatus.NOT_APPLICABLE,
                n,
                f"{n} sessions; annualizing needs at least {MIN_SESSIONS_ANNUAL}",
            )
        )
    dd, dd_pct = max_drawdown((s.equity for s in snaps), capital)
    out.append(
        Metric(
            P,
            "portfolio",
            "max_drawdown",
            dd.quantize(MONEY),
            "USD",
            MetricStatus.OK,
            n,
            "largest fall from a running peak of equity",
        )
    )
    out.append(Metric(P, "portfolio", "max_drawdown_pct", _d(dd_pct), "ratio", MetricStatus.OK, n))
    out += risk_metrics(daily_returns(snaps, capital), P, "portfolio")
    out += risk_metrics(daily_returns(snaps, capital, gross=True), P, "portfolio", "gross_")
    notional = sum((f.notional for f in result.fills), ZERO)
    average_equity = sum((s.equity for s in snaps), ZERO) / n
    out.append(
        Metric(
            P,
            "portfolio",
            "turnover",
            _d(notional / average_equity),
            "ratio",
            MetricStatus.OK,
            n,
            "sum of fill notionals / average equity",
        )
    )
    out.append(
        Metric(
            P,
            "portfolio",
            "traded_notional",
            notional.quantize(MONEY),
            "USD",
            MetricStatus.OK,
            len(result.fills),
        )
    )
    gross_exp = [(s.long_exposure + s.short_exposure) / s.equity for s in snaps if s.equity > 0]
    out.append(
        Metric(
            P,
            "portfolio",
            "average_gross_exposure",
            _d(sum(gross_exp, ZERO) / len(gross_exp)) if gross_exp else None,
            "ratio",
            MetricStatus.OK,
            len(gross_exp),
            "of equity at each valuation",
        )
    )
    proposals = len(result.proposals)
    approved = sum(1 for p in result.proposals if p.risk_result == "PASS_FOR_RESERVATION")
    filled = sum(1 for p in result.proposals if p.trade_seq is not None)
    out += [
        Metric(
            P, "portfolio", "proposals", Decimal(proposals), "count", MetricStatus.OK, proposals
        ),
        Metric(
            P, "portfolio", "risk_approved", Decimal(approved), "count", MetricStatus.OK, proposals
        ),
        Metric(
            P, "portfolio", "entries_filled", Decimal(filled), "count", MetricStatus.OK, approved
        ),
    ]
    out += trade_metrics(result.trades, P, "portfolio")
    return out


def breakdowns(result: BacktestResult) -> list[Metric]:
    out: list[Metric] = []
    groups: dict[tuple[MetricScope, str], list[Trade]] = defaultdict(list)
    for t in result.trades:
        groups[(MetricScope.DIRECTION, t.direction.value)].append(t)
        groups[(MetricScope.STRATEGY, t.strategy_id)].append(t)
        groups[(MetricScope.ASSET, t.symbol)].append(t)
    for p in result.proposals:  # strategies and directions with proposals but no trade
        groups.setdefault((MetricScope.STRATEGY, p.strategy_id), [])
    for (scope, key), trades in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
        out += trade_metrics(trades, scope, key)
        if scope is MetricScope.STRATEGY:
            mine = [p for p in result.proposals if p.strategy_id == key]
            approved = sum(1 for p in mine if p.risk_result == "PASS_FOR_RESERVATION")
            out += [
                Metric(
                    scope, key, "proposals", Decimal(len(mine)), "count", MetricStatus.OK, len(mine)
                ),
                Metric(
                    scope,
                    key,
                    "risk_approved",
                    Decimal(approved),
                    "count",
                    MetricStatus.OK,
                    len(mine),
                ),
                Metric(
                    scope,
                    key,
                    "entries_filled",
                    Decimal(sum(1 for p in mine if p.trade_seq is not None)),
                    "count",
                    MetricStatus.OK,
                    approved,
                ),
            ]
    return out


# ---------------------------------------------------------------------- benchmarks
def _basket_series(
    dataset: HistoricalDataset,
    assets: Sequence[uuid.UUID],
    sessions: Sequence[date],
) -> tuple[list[tuple[date, Decimal]], list[str]]:
    """Value of 1 invested at the first session's open, equally split across ``assets``
    (price return, split-adjusted ratios). An asset without a bar on a session is carried at
    its last close (stated in the notes)."""
    notes: list[str] = []
    first = sessions[0]
    legs: list[tuple[uuid.UUID, Decimal, bool]] = []  # (asset, entry price, split series)
    for a in assets:
        split = dataset.split.get(a, {})
        use_split = bool(split)
        bars = split if use_split else dataset.raw.get(a, {})
        bar = bars.get(first)
        if bar is None or not bar.valid or bar.open <= 0:
            notes.append(f"{a}: no valid bar on {first}, left out")
            continue
        legs.append((a, bar.open, use_split))
    if not legs:
        return [], notes
    weight = Decimal(1) / len(legs)
    last_close: dict[uuid.UUID, Decimal] = {}
    series: list[tuple[date, Decimal]] = []
    carried = 0
    for day in sessions:
        value = ZERO
        for a, entry, use_split in legs:
            bars = dataset.split.get(a, {}) if use_split else dataset.raw.get(a, {})
            bar = bars.get(day)
            if bar is not None and bar.valid:
                last_close[a] = bar.close
            elif a in last_close:
                carried += 1
            price = last_close.get(a, entry)
            value += weight * price / entry
        series.append((day, value))
    if carried:
        notes.append(f"{carried} asset-session(s) without a bar were carried at the last close")
    return series, notes


def benchmark_results(
    result: BacktestResult,
    dataset: HistoricalDataset,
    capital: Decimal,
    *,
    benchmark: tuple[uuid.UUID, str] | None,
    universe: Sequence[uuid.UUID],
) -> list[dict[str, Any]]:
    sessions = [d for d in result.sessions if trading_day(CALENDAR, d) is not None]
    if not sessions or not result.snapshots:
        return []
    portfolio = [s.equity / capital for s in result.snapshots]
    p_returns = daily_returns(result.snapshots, capital)
    out: list[dict[str, Any]] = []
    specs: list[tuple[BenchmarkKind, str, Sequence[uuid.UUID]]] = []
    if benchmark is not None:
        specs.append((BenchmarkKind.BENCHMARK, benchmark[1], [benchmark[0]]))
    if universe:
        specs.append((BenchmarkKind.BUY_AND_HOLD, "EQUAL_WEIGHT_UNIVERSE", list(universe)))
    for kind, label, assets in specs:
        series, notes = _basket_series(dataset, assets, sessions)
        if len(series) != len(portfolio):
            out.append(
                {
                    "kind": kind.value,
                    "label": label,
                    "available": False,
                    "notes": [*notes, "no complete series for the run's sessions"],
                }
            )
            continue
        values = [v for _, v in series]
        returns = []
        previous = Decimal(1)
        for v in values:
            returns.append(float(v / previous - 1))
            previous = v
        total = values[-1] - 1
        _dd, dd_pct = max_drawdown(values, Decimal(1))
        n = len(values)
        metrics = risk_metrics(returns, MetricScope.PORTFOLIO, label)
        annual = (
            _d((1 + float(total)) ** (SESSIONS_PER_YEAR / n) - 1)
            if n >= MIN_SESSIONS_ANNUAL
            else None
        )
        excess = (result.snapshots[-1].equity / capital - 1) - total
        relation: dict[str, Any] = {"status": MetricStatus.INSUFFICIENT_SAMPLE.value}
        if n >= MIN_RETURNS_RISK:
            mb, mp = _mean(returns), _mean(p_returns)
            cov = sum((a - mp) * (b - mb) for a, b in zip(p_returns, returns, strict=True)) / (
                n - 1
            )
            var_b = _stdev(returns) ** 2
            sd_p = _stdev(p_returns)
            relation = {
                "status": MetricStatus.OK.value,
                "beta": None if var_b == 0 else str(_d(cov / var_b)),
                "correlation": None
                if var_b == 0 or sd_p == 0
                else str(_d(cov / (math.sqrt(var_b) * sd_p))),
            }
        out.append(
            {
                "kind": kind.value,
                "label": label,
                "available": True,
                "assets": [str(a) for a in assets],
                "start_session": sessions[0].isoformat(),
                "end_session": sessions[-1].isoformat(),
                "total_return": str(_d(total)),
                "annualized_return": None if annual is None else str(annual),
                "annualized_status": MetricStatus.OK.value
                if annual is not None
                else MetricStatus.NOT_APPLICABLE.value,
                "max_drawdown_pct": str(_d(dd_pct)),
                "excess_return": str(_d(excess)),
                "risk": [m.as_json() for m in metrics],
                "relation": relation,
                "series": [[d.isoformat(), str(_d(v))] for d, v in series],
                "notes": [
                    *notes,
                    "price return, bought at the first session's open, no costs, no dividends",
                ],
            }
        )
    return out


def compute_all(
    result: BacktestResult,
    dataset: HistoricalDataset,
    capital: Decimal,
    *,
    benchmark: tuple[uuid.UUID, str] | None,
    universe: Sequence[uuid.UUID],
) -> None:
    result.metrics = [m.as_json() for m in portfolio_metrics(result, capital) + breakdowns(result)]
    result.benchmarks = benchmark_results(
        result, dataset, capital, benchmark=benchmark, universe=universe
    )


__all__ = [
    "MIN_RETURNS_RISK",
    "MIN_SESSIONS_ANNUAL",
    "MIN_TRADES",
    "Metric",
    "benchmark_results",
    "breakdowns",
    "compute_all",
    "daily_returns",
    "holding_sessions",
    "max_drawdown",
    "portfolio_metrics",
    "risk_metrics",
    "trade_metrics",
]

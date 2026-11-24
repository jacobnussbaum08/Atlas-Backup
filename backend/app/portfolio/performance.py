"""Paper-account performance (spec §22.6): equity curve, drawdown, monthly returns,
risk-adjusted returns, trading costs, strategy and direction breakdowns, benchmark comparison.

Read-only and derived from recorded data only:

* the equity curve is the account's valuation checkpoints (``portfolio_snapshots``), COMPLETE
  ones only, the last of each New York day. STALE or INCOMPLETE checkpoints are counted, never
  drawn, and nothing is interpolated between days;
* trades are the account's **closed simulated paper positions**; their net P/L is realized
  P/L less commissions, fees and accrued borrow (the position's own figures, as when it
  closed);
* the benchmark is the stored split-adjusted daily close of ``benchmark_symbol`` (VALID bars,
  newest revision), on the curve's first and last day only. Price return, no dividends.

Every figure carries its sample size. A ratio is reported only when its sample reaches the
minimum stated in the response; below it the value is ``None`` with the reason. These are the
results of a **simulation** (stored market data, modelled fills), not of real executions.
"""

from __future__ import annotations

import itertools
import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.enums import ValuationStatus
from app.data_engine.enums import DataQualityStatus, PriceAdjustment
from app.database.models import Asset, MarketBarRecord, PortfolioAccount
from app.database.models.execution import PaperPositionRecord
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.execution.enums import PositionStatus

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal(0)
RATIO_PLACES = Decimal("0.000001")
TRADING_DAYS = 252
MIN_RETURNS_FOR_RATIOS = 20  # daily returns before Sharpe / Sortino are shown
DISCLAIMER = (
    "Simulated paper trading: positions filled by the isolated paper broker from stored market "
    "data under a documented execution model. Not real executions, not evidence of future "
    "returns. Benchmark: price return without dividends."
)


@dataclass(frozen=True)
class Point:
    day: date
    equity: Decimal


@dataclass(frozen=True)
class ClosedTrade:
    strategy_id: str | None
    direction: str
    exit_reason: str | None
    opened_at: datetime
    closed_at: datetime
    realized: Decimal
    costs: Decimal

    @property
    def net(self) -> Decimal:
        return self.realized - self.costs


def _ratio(value: float | Decimal | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(Decimal(str(value)).quantize(RATIO_PLACES))


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


# ---------------------------------------------------------------------- pure calculations
def daily_curve(rows: Iterable[tuple[datetime, Decimal, str]]) -> tuple[list[Point], int]:
    """The last COMPLETE checkpoint of each New York day, oldest first, and how many
    checkpoints were left out because their valuation was not complete."""
    by_day: dict[date, tuple[datetime, Decimal]] = {}
    excluded = 0
    for as_of, equity, status in rows:
        if status != ValuationStatus.COMPLETE.value:
            excluded += 1
            continue
        day = as_of.astimezone(NEW_YORK).date()
        if day not in by_day or as_of > by_day[day][0]:
            by_day[day] = (as_of, equity)
    return [Point(d, e) for d, (_, e) in sorted(by_day.items())], excluded


def daily_returns(points: Sequence[Point]) -> list[float]:
    return [float(b.equity / a.equity - 1) for a, b in itertools.pairwise(points) if a.equity > 0]


def drawdowns(points: Sequence[Point]) -> list[tuple[date, Decimal]]:
    """Drawdown from the running peak, as a fraction (0 or negative), per day."""
    out: list[tuple[date, Decimal]] = []
    peak: Decimal | None = None
    for p in points:
        peak = p.equity if peak is None or p.equity > peak else peak
        out.append((p.day, (p.equity / peak - 1) if peak > 0 else ZERO))
    return out


def monthly_returns(points: Sequence[Point]) -> list[dict[str, Any]]:
    """Each calendar month's return from the previous month's last equity (the curve's first
    equity for the first month) to this month's last equity."""
    months: dict[str, Point] = {}
    for p in points:
        months[f"{p.day.year:04d}-{p.day.month:02d}"] = p  # ascending: the last wins
    out: list[dict[str, Any]] = []
    previous = points[0].equity if points else None
    for key, last in months.items():
        if previous is None or previous <= 0:
            continue
        out.append(
            {
                "month": key,
                "return": _ratio(last.equity / previous - 1),
                "end_equity": _money(last.equity),
            }
        )
        previous = last.equity
    return out


def risk_ratios(returns: Sequence[float]) -> dict[str, Any]:
    n = len(returns)
    out: dict[str, Any] = {"daily_returns": n, "minimum": MIN_RETURNS_FOR_RATIOS}
    if n < MIN_RETURNS_FOR_RATIOS:
        out |= {
            "sharpe": None,
            "sortino": None,
            "volatility_annualized": None,
            "reason": f"{n} daily returns; at least {MIN_RETURNS_FOR_RATIOS} are needed",
        }
        return out
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n)
    scale = math.sqrt(TRADING_DAYS)
    out |= {
        "sharpe": _ratio(mean / sd * scale) if sd > 0 else None,
        "sortino": _ratio(mean / downside * scale) if downside > 0 else None,
        "volatility_annualized": _ratio(sd * scale),
        "reason": None,
    }
    return out


def trade_stats(trades: Sequence[ClosedTrade]) -> dict[str, Any]:
    n = len(trades)
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net < 0]
    gross_win, gross_loss = sum(wins, ZERO), sum(losses, ZERO)
    net = sum((t.net for t in trades), ZERO)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _ratio(Decimal(len(wins)) / n) if n else None,
        "net_pnl": _money(net),
        "realized_pnl": _money(sum((t.realized for t in trades), ZERO)),
        "costs": _money(sum((t.costs for t in trades), ZERO)),
        "average_win": _money(gross_win / len(wins)) if wins else None,
        "average_loss": _money(gross_loss / len(losses)) if losses else None,
        "profit_factor": _ratio(gross_win / -gross_loss) if losses else None,
        "expectancy": _money(net / n) if n else None,
    }


def breakdown(trades: Sequence[ClosedTrade], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        groups.setdefault(str(getattr(t, key) or "unattributed"), []).append(t)
    return [{key: name, **trade_stats(group)} for name, group in sorted(groups.items())]


# ---------------------------------------------------------------------- reads
def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=NEW_YORK).astimezone(UTC)


def _benchmark_close(session: Session, asset_id: uuid.UUID, day: date) -> Decimal | None:
    b = MarketBarRecord
    row = session.scalar(
        select(b)
        .where(
            b.asset_id == asset_id,
            b.timeframe == "1Day",
            b.adjustment == PriceAdjustment.SPLIT,
            b.event_time == _midnight(day),
        )
        .order_by(b.revision.desc())
        .limit(1)
    )
    if row is None or row.quality_status is not DataQualityStatus.VALID:
        return None  # the newest revision decides; a quarantined one hides older ones
    return row.close


def benchmark(session: Session, symbol: str, points: Sequence[Point]) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol, "return": None, "start": None, "end": None}
    if len(points) < 2:
        return out | {"reason": "the equity curve needs at least two days"}
    asset_id = session.scalar(select(Asset.id).where(Asset.symbol == symbol).limit(1))
    if asset_id is None:
        return out | {"reason": f"{symbol} is not a known asset"}
    first, last = points[0].day, points[-1].day
    start = _benchmark_close(session, asset_id, first)
    end = _benchmark_close(session, asset_id, last)
    if start is None or end is None or start <= 0:
        return out | {"reason": f"no VALID stored daily close of {symbol} on {first} and {last}"}
    account = points[-1].equity / points[0].equity - 1
    bench = end / start - 1
    return out | {
        "return": _ratio(bench),
        "start": first.isoformat(),
        "end": last.isoformat(),
        "account_return_same_period": _ratio(account),
        "excess_return": _ratio(account - bench),
        "reason": None,
    }


def report(
    session: Session, account: PortfolioAccount, *, benchmark_symbol: str = "SPY"
) -> dict[str, Any]:
    s = PortfolioSnapshotRecord
    rows = session.execute(
        select(s.as_of, s.equity, s.valuation_status)
        .where(s.portfolio_account_id == account.id)
        .order_by(s.as_of)
    ).all()
    points, excluded = daily_curve((r[0], r[1], str(r[2].value)) for r in rows)
    p = PaperPositionRecord
    positions = session.scalars(
        select(p)
        .where(p.portfolio_account_id == account.id, p.status == PositionStatus.CLOSED)
        .order_by(p.closed_at)
    ).all()
    trades = [
        ClosedTrade(
            strategy_id=x.strategy_id,
            direction=x.direction.value,
            exit_reason=None if x.exit_reason is None else x.exit_reason.value,
            opened_at=x.opened_at,
            closed_at=x.closed_at or x.opened_at,
            realized=x.realized_pnl,
            costs=x.commissions + x.fees + x.borrow_accrued,
        )
        for x in positions
    ]
    dd = drawdowns(points)
    returns = daily_returns(points)
    total = points[-1].equity / points[0].equity - 1 if len(points) >= 2 else None
    return {
        "account_id": str(account.id),
        "account_name": account.name,
        "currency": account.base_currency,
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "curve": {
            "days": len(points),
            "checkpoints_excluded": excluded,
            "first_day": points[0].day.isoformat() if points else None,
            "last_day": points[-1].day.isoformat() if points else None,
            "points": [{"day": x.day.isoformat(), "equity": _money(x.equity)} for x in points],
            "drawdown": [{"day": d.isoformat(), "drawdown": _ratio(v)} for d, v in dd],
        },
        "summary": {
            "total_return": _ratio(total),
            "max_drawdown": _ratio(min((v for _, v in dd), default=None)),
            "current_drawdown": _ratio(dd[-1][1]) if dd else None,
            **risk_ratios(returns),
        },
        "monthly_returns": monthly_returns(points),
        "trades": trade_stats(trades),
        "by_strategy": breakdown(trades, "strategy_id"),
        "by_direction": breakdown(trades, "direction"),
        "by_exit_reason": breakdown(trades, "exit_reason"),
        "recent_trades": [
            {
                "strategy_id": t.strategy_id,
                "direction": t.direction,
                "exit_reason": t.exit_reason,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
                "holding_days": (t.closed_at - t.opened_at) / timedelta(days=1),
                "net_pnl": _money(t.net),
                "costs": _money(t.costs),
            }
            for t in trades[-50:][::-1]
        ],
        "benchmark": benchmark(session, benchmark_symbol, points),
    }


__all__ = [
    "DISCLAIMER",
    "MIN_RETURNS_FOR_RATIOS",
    "ClosedTrade",
    "Point",
    "benchmark",
    "breakdown",
    "daily_curve",
    "daily_returns",
    "drawdowns",
    "monthly_returns",
    "report",
    "risk_ratios",
    "trade_stats",
]

"""Paper-trading evidence (Phase 11A): what the live system actually did in paper mode.

Everything here is read from the records the running system wrote: proposals, AI decisions,
trade intents, risk assessments, orders, simulated fills, paper positions, portfolio
snapshots, reconciliations, alerts and orchestration jobs. Nothing is replayed or estimated,
and backtests never count as paper evidence.

* **Trades** are CLOSED paper positions, net of commissions, fees and accrued borrow (the
  8B monitor's definition). Per-trade return = net P/L / entry value.
* **Observed sessions** are XNYS sessions on which the orchestrator ran at least one
  scheduled job successfully: a session with nothing running is not observation.
* **Drawdown** (per strategy) is the drawdown of initial capital plus cumulative closed-trade
  net P/L in closing order; the account's drawdown comes from its valued snapshots.
* **Execution quality**: each entry fill against the decision's reference price
  (``strategy_proposals.reference_entry``), signed so that positive is adverse, and against
  the arrival price the paper broker recorded; the execution model's assumed slippage plus
  half spread is the backtest's assumption. Stop exits are measured against the stop level.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.asset_discovery.sessions import local_date, trading_day
from app.backtesting.data import CALENDAR
from app.database.models import (
    AIDecisionRecord,
    ExecutionFillDetailRecord,
    OrchestrationJobRecord,
    OrderRecord,
    PaperPositionRecord,
    PortfolioSnapshotRecord,
    RiskAssessmentRecord,
    StrategyProposalRecord,
    SystemHealthEventRecord,
    TradeIntentRecord,
)
from app.database.models.accounting import ReconciliationRunRecord
from app.database.models.execution import ExecutionModelRecord
from app.database.models.reservations import RiskReservationReconciliationRecord
from app.evidence.classify import Paper, SectionStats
from app.orchestration import reliability

BPS = Decimal(10_000)


@dataclass(frozen=True)
class ClosedTrade:
    strategy_id: str
    symbol: str
    direction: str
    opened_at: datetime
    closed_at: datetime
    net_pnl: float
    costs: float
    entry_value: float
    exit_reason: str | None

    @property
    def ret(self) -> float:
        return self.net_pnl / self.entry_value if self.entry_value > 0 else 0.0


def closed_trades(
    session: Session, account_id: uuid.UUID, since: datetime | None, until: datetime
) -> list[ClosedTrade]:
    p = PaperPositionRecord
    q = (
        select(p)
        .where(
            p.portfolio_account_id == account_id,
            p.status == "CLOSED",
            p.closed_at <= until,
        )
        .order_by(p.closed_at, p.id)
    )
    if since is not None:
        q = q.where(p.opened_at >= since)
    out: list[ClosedTrade] = []
    for pos in session.scalars(q):
        assert pos.closed_at is not None  # noqa: S101  # status CLOSED
        costs = pos.commissions + pos.fees + pos.borrow_accrued
        out.append(
            ClosedTrade(
                strategy_id=pos.strategy_id or "UNATTRIBUTED",
                symbol=pos.symbol,
                direction=pos.direction.value,
                opened_at=pos.opened_at,
                closed_at=pos.closed_at,
                net_pnl=float(pos.realized_pnl - costs),
                costs=float(costs),
                entry_value=float(pos.entry_value),
                exit_reason=None if pos.exit_reason is None else pos.exit_reason.value,
            )
        )
    return out


def _stats_of(trades: list[ClosedTrade], sessions: int, capital: float) -> SectionStats:
    pnls = [t.net_pnl for t in trades]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    total = sum(pnls)
    by_asset: dict[str, float] = defaultdict(float)
    for t in trades:
        by_asset[t.symbol] += t.net_pnl
    top = sorted(wins, reverse=True)[:5]
    equity, peak, dd = capital, capital, 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        if peak > 0:
            dd = max(dd, (peak - equity) / peak)
    by_direction: dict[str, dict[str, Any]] = {}
    for d in ("LONG", "SHORT"):
        sub = [t.net_pnl for t in trades if t.direction == d]
        if sub:
            by_direction[d] = {
                "trades": len(sub),
                "net_pnl": sum(sub),
                "expectancy": sum(sub) / len(sub),
            }
    return SectionStats(
        trades=len(trades),
        sessions=sessions,
        net_pnl=total,
        gross_pnl=total + sum(t.costs for t in trades),
        costs=sum(t.costs for t in trades),
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        expectancy=total / len(pnls) if pnls else None,
        win_rate=len(wins) / len(pnls) if pnls else None,
        top5_share=sum(top) / total if total > 0 else None,
        largest_asset_share=max(by_asset.values()) / total if total > 0 and by_asset else None,
        drawdown=dd if trades else None,
        net_return=total / capital if capital > 0 else None,
        returns=tuple(t.ret for t in trades),
        by_direction=by_direction,
    )


def observed_sessions(session: Session, since: datetime | None, until: datetime) -> list[str]:
    j = OrchestrationJobRecord
    q = select(j.finished_at).where(j.status == "SUCCEEDED", j.finished_at <= until)
    if since is not None:
        q = q.where(j.finished_at >= since)
    days: set[str] = set()
    for (at,) in session.execute(q):
        if at is None:
            continue
        day = local_date(CALENDAR, at)
        sday = trading_day(CALENDAR, day)
        if sday is not None and sday.regular_open <= at <= sday.regular_close:
            days.add(day.isoformat())
    return sorted(days)


def execution_quality(
    session: Session, account_id: uuid.UUID, since: datetime | None, until: datetime
) -> dict[str, dict[str, Any]]:
    """Per strategy: entry shortfall vs the decision reference and vs arrival, assumed cost
    of the execution model, stop-exit slippage, gap and partial fills."""
    f, o, pr, m = (
        ExecutionFillDetailRecord,
        OrderRecord,
        StrategyProposalRecord,
        ExecutionModelRecord,
    )
    q = (
        select(f, o, pr.reference_entry, pr.strategy_id, m.definition)
        .join(o, o.id == f.order_id)
        .outerjoin(pr, pr.id == o.strategy_proposal_id)
        .join(m, m.id == f.execution_model_id)
        .where(f.portfolio_account_id == account_id, f.filled_at <= until)
        .order_by(f.filled_at)
    )
    if since is not None:
        q = q.where(f.filled_at >= since)
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    fills_per_order: Counter[uuid.UUID] = Counter()
    order_strategy: dict[uuid.UUID, str] = {}
    pos_strategy = {
        r.entry_order_id: r.strategy_id or "UNATTRIBUTED"
        for r in session.scalars(
            select(PaperPositionRecord).where(
                PaperPositionRecord.portfolio_account_id == account_id
            )
        )
    }
    for fill, order, reference, proposal_strategy, definition in session.execute(q):
        key = proposal_strategy or pos_strategy.get(order.id) or "UNATTRIBUTED"
        order_strategy[order.id] = key
        fills_per_order[order.id] += 1
        buy = fill.side.value in ("BUY", "BUY_TO_COVER")
        price = fill.price
        purpose = order.purpose.value
        counts[key][f"{purpose}_fills"] += 1
        if fill.gap:
            counts[key]["gap_fills"] += 1
        if purpose == "ENTRY":
            if reference is not None and reference > 0:
                adverse = (
                    (price - reference) / reference if buy else (reference - price) / reference
                )
                acc[key]["entry_vs_decision_bps"].append(float(adverse * BPS))
            if fill.shortfall_bps is not None:
                acc[key]["entry_vs_arrival_bps"].append(float(fill.shortfall_bps))
            d = definition or {}
            assumed = Decimal(str(d.get("slippage_bps", 0))) + Decimal(
                str(d.get("assumed_half_spread_bps", 0))
            )
            acc[key]["assumed_cost_bps"].append(float(assumed))
        elif order.stop_price is not None and order.stop_price > 0:
            stop = order.stop_price
            adverse = (stop - price) / stop if not buy else (price - stop) / stop
            acc[key]["stop_exit_vs_stop_bps"].append(float(adverse * BPS))
    for order_id, n in fills_per_order.items():
        if n > 1:
            counts[order_strategy[order_id]]["partially_filled_orders"] += 1
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(set(acc) | set(counts)):
        row: dict[str, Any] = dict(counts[key])
        for name, values in acc[key].items():
            row[name] = {
                "n": len(values),
                "mean": sum(values) / len(values) if values else None,
                "max": max(values) if values else None,
            }
        out[key] = row
    return out


def _count_by(session: Session, column: Any, *where: Any) -> dict[str, int]:
    rows = session.execute(select(column, func.count()).where(*where).group_by(column)).all()
    return {str(getattr(k, "value", k)): int(n) for k, n in rows}


def funnel(
    session: Session, account_id: uuid.UUID, since: datetime | None, until: datetime
) -> dict[str, Any]:
    """From proposal to exit: what was proposed, decided, assessed, ordered and closed."""

    def window(col: Any) -> list[Any]:
        w = [col <= until]
        if since is not None:
            w.append(col >= since)
        return w

    pr, ai, ti = StrategyProposalRecord, AIDecisionRecord, TradeIntentRecord
    ra, o, pp = RiskAssessmentRecord, OrderRecord, PaperPositionRecord
    rejected_by: Counter[str] = Counter()
    for (constraint,) in session.execute(
        select(ra.binding_constraint).where(
            ra.portfolio_account_id == account_id,
            ra.result != "PASS_FOR_RESERVATION",
            *window(ra.assessed_at),
        )
    ):
        rejected_by[constraint or "UNSPECIFIED"] += 1
    return {
        "proposals_by_strategy": _count_by(session, pr.strategy_id, *window(pr.created_at)),
        "ai_decisions_by_outcome": _count_by(session, ai.outcome, *window(ai.decision_at)),
        "intents_by_status": _count_by(
            session, ti.status, ti.portfolio_account_id == account_id, *window(ti.received_at)
        ),
        "risk_assessments_by_result": _count_by(
            session, ra.result, ra.portfolio_account_id == account_id, *window(ra.assessed_at)
        ),
        "risk_rejections_by_constraint": dict(rejected_by),
        "orders_by_status": _count_by(
            session, o.status, o.portfolio_account_id == account_id, *window(o.submitted_at)
        ),
        "positions_by_status": _count_by(
            session, pp.status, pp.portfolio_account_id == account_id, *window(pp.opened_at)
        ),
        "exits_by_reason": _count_by(
            session,
            pp.exit_reason,
            pp.portfolio_account_id == account_id,
            pp.status == "CLOSED",
            *window(pp.opened_at),
        ),
    }


def account_drawdown(
    session: Session, account_id: uuid.UUID, since: datetime | None, until: datetime
) -> dict[str, Any]:
    s = PortfolioSnapshotRecord
    q = (
        select(s.as_of, s.equity, s.valuation_status)
        .where(s.portfolio_account_id == account_id, s.as_of <= until)
        .order_by(s.as_of)
    )
    if since is not None:
        q = q.where(s.as_of >= since)
    peak, dd, first, last, n = None, 0.0, None, None, 0
    for _, equity, status in session.execute(q):
        if str(getattr(status, "value", status)) != "COMPLETE":
            continue
        e = float(equity)
        n += 1
        first = e if first is None else first
        last = e
        peak = e if peak is None else max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak)
    return {
        "valued_snapshots": n,
        "first_equity": first,
        "last_equity": last,
        "return": None if not first or last is None else last / first - 1,
        "max_drawdown": dd if n else None,
    }


def reconciliation(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    acc = session.scalars(
        select(ReconciliationRunRecord)
        .where(ReconciliationRunRecord.portfolio_account_id == account_id)
        .order_by(ReconciliationRunRecord.finished_at.desc())
        .limit(1)
    ).first()
    res = session.scalars(
        select(RiskReservationReconciliationRecord)
        .where(RiskReservationReconciliationRecord.portfolio_account_id == account_id)
        .order_by(RiskReservationReconciliationRecord.finished_at.desc())
        .limit(1)
    ).first()
    j = OrchestrationJobRecord
    execu = session.scalars(
        select(j)
        .where(j.kind == "EXECUTION_RECONCILE", j.status.in_(["SUCCEEDED", "FAILED", "DEAD"]))
        .order_by(j.finished_at.desc())
        .limit(1)
    ).first()
    acc_ok = None if acc is None else acc.status.value == "PASSED"
    res_ok = None if res is None else res.unexplained == 0
    exec_ok = None if execu is None else execu.status.value == "SUCCEEDED"
    parts = [x for x in (acc_ok, res_ok, exec_ok) if x is not None]
    return {
        "accounting": None
        if acc is None
        else {
            "status": acc.status.value,
            "failed_checks": acc.checks_failed,
            "at": acc.finished_at.isoformat(),
        },
        "reservations": None
        if res is None
        else {
            "status": res.status.value,
            "unexplained": res.unexplained,
            "at": res.finished_at.isoformat(),
        },
        "execution": None
        if execu is None
        else {
            "status": execu.status.value,
            "at": None if execu.finished_at is None else execu.finished_at.isoformat(),
        },
        # None when nothing was ever reconciled; True only if every recorded kind passed.
        "clean": None if len(parts) < 3 else all(parts),
    }


def open_critical_alerts(session: Session, account_id: uuid.UUID) -> int:
    return len(
        reliability.alerts(session, account_id, min_severity="CRITICAL", open_only=True, limit=500)
    )


def alerts_by_type(
    session: Session, account_id: uuid.UUID, since: datetime | None, until: datetime
) -> dict[str, int]:
    h = SystemHealthEventRecord
    w = [
        h.occurred_at <= until,
        (h.portfolio_account_id == account_id) | h.portfolio_account_id.is_(None),
    ]
    if since is not None:
        w.append(h.occurred_at >= since)
    return _count_by(session, h.event_type, *w)


def strategy_paper(
    trades: list[ClosedTrade],
    strategy_id: str,
    sessions: int,
    capital: float,
    quality: dict[str, Any] | None,
) -> Paper:
    mine = [t for t in trades if t.strategy_id == strategy_id]
    q = quality or {}
    vs_decision = q.get("entry_vs_decision_bps") or {}
    assumed = q.get("assumed_cost_bps") or {}
    return Paper(
        stats=_stats_of(mine, sessions, capital),
        entry_shortfall_bps=vs_decision.get("mean"),
        assumed_cost_bps=assumed.get("mean"),
        entry_fills=int(q.get("ENTRY_fills", 0)),
    )


def portfolio_trades_stats(
    trades: list[ClosedTrade], sessions: int, capital: float
) -> SectionStats:
    return _stats_of(trades, sessions, capital)


__all__ = [
    "ClosedTrade",
    "account_drawdown",
    "alerts_by_type",
    "closed_trades",
    "execution_quality",
    "funnel",
    "observed_sessions",
    "open_critical_alerts",
    "portfolio_trades_stats",
    "reconciliation",
    "strategy_paper",
]

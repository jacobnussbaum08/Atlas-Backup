"""Read models of backtest runs for the API, the CLI and the dashboard (Phase 8A).

Everything returned is read from the stored run and its result rows; nothing is
recomputed or estimated here. Decimals are strings.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtesting.enums import RunStatus, UniverseMode
from app.core.exceptions import NotFoundError
from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    Asset,
    AssetClass,
    BacktestBenchmarkResultRecord,
    BacktestEquitySnapshotRecord,
    BacktestEvaluationCountRecord,
    BacktestEventCursorRecord,
    BacktestFillRecord,
    BacktestJournalEntryRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestProposalRecord,
    BacktestRejectionRecord,
    BacktestRunErrorRecord,
    BacktestRunEventRecord,
    BacktestRunRecord,
    BacktestStrategyVersionRecord,
    BacktestTradeRecord,
    MarketBarRecord,
)
from app.feature_engine.registry import DAILY
from app.strategies import catalog
from app.strategies.registry import StrategyRegistry


def _v(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row(record: Any, skip: tuple[str, ...] = ("run_id",)) -> dict[str, Any]:
    return {
        c.key: _v(getattr(record, c.key)) for c in record.__table__.columns if c.key not in skip
    }


def _run(session: Session, run_id: uuid.UUID) -> BacktestRunRecord:
    run = session.get(BacktestRunRecord, run_id)
    if run is None:
        raise NotFoundError(f"backtest run {run_id} does not exist")
    return run


def run_summary(run: BacktestRunRecord) -> dict[str, Any]:
    strategies = [s["strategy_id"] for s in run.config.get("strategies", [])]
    progress = None if not run.sessions_total else round(run.sessions_done / run.sessions_total, 4)
    return {
        "id": str(run.id),
        "name": run.name,
        "status": run.status.value,
        "simulated": run.simulated,
        "start_date": run.start_date.isoformat(),
        "end_date": run.end_date.isoformat(),
        "initial_capital": str(run.initial_capital),
        "currency": run.currency,
        "strategies": strategies,
        "universe_mode": run.universe_mode,
        "symbols": [a["symbol"] for a in run.config.get("assets", [])][:50],
        "asset_count": len(run.config.get("assets", [])),
        "benchmark_symbol": run.benchmark_symbol,
        "seed": run.seed,
        "created_at": run.created_at.isoformat(),
        "started_at": None if run.started_at is None else run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "sessions_total": run.sessions_total,
        "sessions_done": run.sessions_done,
        "progress": progress,
        "simulated_through": None
        if run.simulated_through is None
        else run.simulated_through.isoformat(),
        "result_sha256": run.result_sha256,
        "reproduction_of": None if run.reproduction_of is None else str(run.reproduction_of),
        "reproduction_matches": run.reproduction_matches,
        "error_stage": run.error_stage,
        "error_message": run.error_message,
        "summary": run.summary,
        "requested_by": run.requested_by,
    }


def runs(
    session: Session, *, limit: int = 50, status: RunStatus | None = None
) -> list[dict[str, Any]]:
    query = select(BacktestRunRecord).order_by(BacktestRunRecord.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(BacktestRunRecord.status == status)
    return [run_summary(r) for r in session.scalars(query)]


def run_detail(session: Session, run_id: uuid.UUID) -> dict[str, Any]:
    run = _run(session, run_id)
    versions = session.scalars(
        select(BacktestStrategyVersionRecord)
        .where(BacktestStrategyVersionRecord.run_id == run_id)
        .order_by(BacktestStrategyVersionRecord.strategy_id)
    ).all()
    errors = session.scalar(select(func.count()).where(BacktestRunErrorRecord.run_id == run_id))
    return {
        **run_summary(run),
        "request": run.request,
        "config": run.config,
        "config_sha256": run.config_sha256,
        "engine_version": run.engine_version,
        "code_version": run.code_version,
        "data_provider": run.data_provider,
        "data_feed": run.data_feed,
        "data_known_at": run.data_known_at.isoformat(),
        "dataset_sha256": run.dataset_sha256,
        "dataset_bars": run.dataset_bars,
        "execution_model_id": run.execution_model_id,
        "execution_model_sha256": run.execution_model_sha256,
        "risk_sha256": run.risk_sha256,
        "risk_source": run.risk_source,
        "strategy_versions": [_row(v) for v in versions],
        "errors": errors or 0,
    }


def _rows(
    session: Session,
    model: Any,
    run_id: uuid.UUID,
    order: Any,
    limit: int,
    offset: int = 0,
    **filters: Any,
) -> list[dict[str, Any]]:
    _run(session, run_id)
    query = select(model).where(model.run_id == run_id)
    for key, value in filters.items():
        if value is not None:
            query = query.where(getattr(model, key) == value)
    query = query.order_by(order).offset(offset).limit(limit)
    return [_row(r) for r in session.scalars(query)]


def cursors(session: Session, run_id: uuid.UUID, limit: int = 5000) -> list[dict[str, Any]]:
    return _rows(session, BacktestEventCursorRecord, run_id, BacktestEventCursorRecord.seq, limit)


def equity(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        session, BacktestEquitySnapshotRecord, run_id, BacktestEquitySnapshotRecord.session, 10000
    )


def trades(
    session: Session,
    run_id: uuid.UUID,
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    return _rows(
        session,
        BacktestTradeRecord,
        run_id,
        BacktestTradeRecord.seq,
        limit,
        strategy_id=strategy_id,
        symbol=symbol,
    )


def trade_detail(session: Session, run_id: uuid.UUID, seq: int) -> dict[str, Any]:
    trade = session.get(BacktestTradeRecord, (run_id, seq))
    if trade is None:
        raise NotFoundError(f"trade {seq} of backtest run {run_id} does not exist")
    order_seqs = [
        s
        for s in (
            trade.details.get("entry_order_seq"),
            trade.details.get("stop_order_seq"),
            trade.details.get("tp_order_seq"),
            *trade.details.get("exit_order_seqs", []),
        )
        if s
    ]
    orders = session.scalars(
        select(BacktestOrderRecord)
        .where(BacktestOrderRecord.run_id == run_id, BacktestOrderRecord.seq.in_(order_seqs))
        .order_by(BacktestOrderRecord.seq)
    ).all()
    fills = session.scalars(
        select(BacktestFillRecord)
        .where(BacktestFillRecord.run_id == run_id, BacktestFillRecord.order_seq.in_(order_seqs))
        .order_by(BacktestFillRecord.seq)
    ).all()
    proposal = session.get(BacktestProposalRecord, (run_id, trade.proposal_seq))
    return {
        "trade": _row(trade),
        "proposal": None if proposal is None else _row(proposal),
        "orders": [_row(o) for o in orders],
        "fills": [_row(f) for f in fills],
    }


def orders(session: Session, run_id: uuid.UUID, *, limit: int = 1000) -> list[dict[str, Any]]:
    return _rows(session, BacktestOrderRecord, run_id, BacktestOrderRecord.seq, limit)


def fills(session: Session, run_id: uuid.UUID, *, limit: int = 2000) -> list[dict[str, Any]]:
    return _rows(session, BacktestFillRecord, run_id, BacktestFillRecord.seq, limit)


def proposals(
    session: Session,
    run_id: uuid.UUID,
    *,
    strategy_id: str | None = None,
    outcome: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    return _rows(
        session,
        BacktestProposalRecord,
        run_id,
        BacktestProposalRecord.seq,
        limit,
        strategy_id=strategy_id,
        outcome=outcome,
    )


def rejections(
    session: Session, run_id: uuid.UUID, *, stage: str | None = None, limit: int = 2000
) -> list[dict[str, Any]]:
    return _rows(
        session, BacktestRejectionRecord, run_id, BacktestRejectionRecord.seq, limit, stage=stage
    )


def metrics(
    session: Session, run_id: uuid.UUID, *, scope: str | None = None
) -> list[dict[str, Any]]:
    _run(session, run_id)
    query = select(BacktestMetricRecord).where(BacktestMetricRecord.run_id == run_id)
    if scope is not None:
        query = query.where(BacktestMetricRecord.scope == scope)
    query = query.order_by(
        BacktestMetricRecord.scope, BacktestMetricRecord.scope_key, BacktestMetricRecord.metric
    )
    return [_row(m) for m in session.scalars(query)]


def benchmarks(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        session, BacktestBenchmarkResultRecord, run_id, BacktestBenchmarkResultRecord.kind, 10
    )


def events(session: Session, run_id: uuid.UUID, limit: int = 2000) -> list[dict[str, Any]]:
    return _rows(session, BacktestRunEventRecord, run_id, BacktestRunEventRecord.seq, limit)


def errors(session: Session, run_id: uuid.UUID, limit: int = 500) -> list[dict[str, Any]]:
    return _rows(session, BacktestRunErrorRecord, run_id, BacktestRunErrorRecord.seq, limit)


def journal(
    session: Session, run_id: uuid.UUID, *, offset: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    return _rows(
        session,
        BacktestJournalEntryRecord,
        run_id,
        BacktestJournalEntryRecord.sequence,
        limit,
        offset,
    )


def evaluations(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        session,
        BacktestEvaluationCountRecord,
        run_id,
        BacktestEvaluationCountRecord.strategy_id,
        1000,
    )


COMPARE_METRICS = (
    "total_return",
    "gross_total_return",
    "net_pnl",
    "max_drawdown_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "annualized_return",
    "win_rate",
    "profit_factor",
    "expectancy",
    "trades_closed",
    "total_costs",
    "turnover",
)


def compare(session: Session, run_ids: list[uuid.UUID]) -> dict[str, Any]:
    """Selected portfolio metrics of several runs side by side, and strategy-scope rows."""
    out: list[dict[str, Any]] = []
    for run_id in run_ids:
        run = _run(session, run_id)
        rows = session.scalars(
            select(BacktestMetricRecord).where(BacktestMetricRecord.run_id == run_id)
        ).all()
        portfolio = {
            m.metric: {"value": _v(m.value), "status": m.status, "sample_size": m.sample_size}
            for m in rows
            if m.scope == "PORTFOLIO" and m.metric in COMPARE_METRICS
        }
        strategies: dict[str, dict[str, Any]] = {}
        for m in rows:
            if m.scope == "STRATEGY" and m.metric in (*COMPARE_METRICS, "net_pnl_trades"):
                strategies.setdefault(m.scope_key, {})[m.metric] = {
                    "value": _v(m.value),
                    "status": m.status,
                    "sample_size": m.sample_size,
                }
        bench = session.scalars(
            select(BacktestBenchmarkResultRecord).where(
                BacktestBenchmarkResultRecord.run_id == run_id
            )
        ).all()
        out.append(
            {
                "run": run_summary(run),
                "portfolio": portfolio,
                "strategies": strategies,
                "benchmarks": [
                    {
                        "kind": b.kind,
                        "label": b.label,
                        "total_return": _v(b.total_return),
                        "excess_return": _v(b.excess_return),
                    }
                    for b in bench
                ],
            }
        )
    return {"runs": out, "metrics": list(COMPARE_METRICS)}


def options(
    session: Session, registry: StrategyRegistry, *, provider: str, feed: str
) -> dict[str, Any]:
    """What a backtest can be configured with: strategy versions, assets with stored daily
    bars (and their ranges), universe layers."""
    strategies = []
    for strategy in sorted(registry, key=lambda s: s.strategy_id):
        version = catalog.latest_version(session, strategy.strategy_id)
        params = None
        if version is not None:
            params = catalog.configuration(session, version).parameters
        strategies.append(
            {
                "strategy_id": strategy.strategy_id,
                "family": strategy.family.value,
                "name": strategy.name,
                "implemented": strategy.implemented,
                "supported_timeframes": list(strategy.supported_timeframes),
                "latest_version": None if version is None else version.version,
                "logic_version": strategy.logic_version,
                "replayable": version is not None
                and version.logic_version == strategy.logic_version,
                "parameters": params,
                "parameters_schema": strategy.parameters_schema(),
            }
        )
    b = MarketBarRecord
    ranges = session.execute(
        select(
            b.asset_id,
            b.adjustment,
            func.min(b.event_time),
            func.max(b.event_time),
            func.count(),
        )
        .where(
            b.timeframe == DAILY,
            b.provider == provider,
            b.feed == feed,
            b.adjustment.in_([PriceAdjustment.RAW, PriceAdjustment.SPLIT]),
        )
        .group_by(b.asset_id, b.adjustment)
    ).all()
    by_asset: dict[uuid.UUID, dict[str, Any]] = {}
    for asset_id, adjustment, first, last, count in ranges:
        by_asset.setdefault(asset_id, {})[adjustment.value] = {
            "first": first.date().isoformat(),
            "last": last.date().isoformat(),
            "bars": count,
        }
    assets = session.scalars(
        select(Asset).where(
            Asset.id.in_(list(by_asset)),
            Asset.asset_class.in_([AssetClass.EQUITY, AssetClass.ETF]),
        )
    ).all()
    return {
        "strategies": strategies,
        "timeframes": ["1Day", "1Week"],
        "universe_modes": [m.value for m in UniverseMode],
        "layers": ["DISCOVERY_POOL", "QUALIFIED_UNIVERSE", "ACTIVE_WATCHLIST"],
        "assets": sorted(
            (
                {
                    "asset_id": str(a.id),
                    "symbol": a.symbol,
                    "asset_class": a.asset_class.value,
                    "raw": by_asset[a.id].get("RAW"),
                    "split": by_asset[a.id].get("SPLIT"),
                }
                for a in assets
            ),
            key=lambda x: x["symbol"],
        ),
        "data": {"provider": provider, "feed": feed},
    }


__all__ = [
    "benchmarks",
    "compare",
    "cursors",
    "equity",
    "errors",
    "evaluations",
    "events",
    "fills",
    "journal",
    "metrics",
    "options",
    "orders",
    "proposals",
    "rejections",
    "run_detail",
    "run_summary",
    "runs",
    "trade_detail",
    "trades",
]

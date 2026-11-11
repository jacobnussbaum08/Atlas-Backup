"""Backtest runs and their results (Phase 8A, migration 0018).

A run row holds the request and the fully resolved configuration (strategy versions, risk
configuration, execution model, dataset knowledge time, policies, seed, code version). Its
status moves only QUEUED -> RUNNING -> COMPLETED | FAILED; configuration columns never
change, and a final run never changes at all (database trigger). Result rows are written
only while their run is RUNNING and are never updated or deleted, so a completed run's
records are immutable. Everything is a simulation over stored historical data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backtesting.enums import RunStatus
from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum

_RUN = "backtest_runs.id"


def _run_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey(_RUN, ondelete="RESTRICT"), primary_key=True)


class BacktestRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="date_order"),
        CheckConstraint("initial_capital > 0", name="capital_positive"),
        CheckConstraint("config_sha256 ~ '^[0-9a-f]{64}$'", name="config_sha_format"),
        CheckConstraint("simulated", name="always_simulated"),
        CheckConstraint(
            "(status IN ('COMPLETED')) = (result_sha256 IS NOT NULL)", name="result_when_completed"
        ),
        CheckConstraint("status <> 'FAILED' OR error_message IS NOT NULL", name="failed_has_error"),
        CheckConstraint("sessions_done <= sessions_total", name="progress_bounded"),
        Index(None, "created_at"),
        Index(None, "status"),
    )

    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "backtest_run_status"))
    simulated: Mapped[bool] = mapped_column(Boolean, server_default="true")
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(16))
    code_version: Mapped[str] = mapped_column(String(80))
    seed: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    initial_capital: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3))
    universe_mode: Mapped[str] = mapped_column(String(16))
    benchmark_symbol: Mapped[str | None] = mapped_column(String(64))
    data_provider: Mapped[str] = mapped_column(String(32))
    data_feed: Mapped[str] = mapped_column(String(32))
    data_known_at: Mapped[datetime]
    execution_model_id: Mapped[int] = mapped_column(
        ForeignKey("execution_models.id", ondelete="RESTRICT")
    )
    execution_model_sha256: Mapped[str] = mapped_column(String(64))
    risk_sha256: Mapped[str] = mapped_column(String(64))
    risk_source: Mapped[str] = mapped_column(String(80))
    reproduction_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(_RUN, ondelete="RESTRICT"))
    requested_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    sessions_total: Mapped[int] = mapped_column(Integer, server_default="0")
    sessions_done: Mapped[int] = mapped_column(Integer, server_default="0")
    simulated_through: Mapped[datetime | None]
    dataset_sha256: Mapped[str | None] = mapped_column(String(64))
    dataset_bars: Mapped[int | None] = mapped_column(Integer)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    reproduction_matches: Mapped[bool | None] = mapped_column(Boolean)
    error_stage: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class BacktestStrategyVersionRecord(Base):
    """The immutable strategy version (and effective parameters) a run replayed."""

    __tablename__ = "backtest_strategy_versions"

    run_id: Mapped[uuid.UUID] = _run_fk()
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("strategies.id", ondelete="RESTRICT"), primary_key=True
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    logic_version: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters_sha256: Mapped[str] = mapped_column(String(64))
    parameters_source: Mapped[str] = mapped_column(String(32))
    timeframes: Mapped[list[str]] = mapped_column(ARRAY(String(16)))


class BacktestEventCursorRecord(Base):
    """Replay progress: one row per processed session (written while the run is RUNNING)."""

    __tablename__ = "backtest_event_cursors"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    session: Mapped[date] = mapped_column(Date)
    simulated_at: Mapped[datetime]
    sessions_done: Mapped[int] = mapped_column(Integer)
    sessions_total: Mapped[int] = mapped_column(Integer)
    evaluations: Mapped[int] = mapped_column(Integer)
    proposals: Mapped[int] = mapped_column(Integer)
    fills: Mapped[int] = mapped_column(Integer)
    journal_entries: Mapped[int] = mapped_column(Integer)
    reconciliation: Mapped[str] = mapped_column(String(16))
    equity: Mapped[Decimal]
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BacktestProposalRecord(Base):
    """A strategy proposal produced during the replay, with its risk decision and fate."""

    __tablename__ = "backtest_proposals"
    __table_args__ = (Index(None, "run_id", "strategy_id"),)

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    decided_at: Mapped[datetime]
    session: Mapped[date] = mapped_column(Date)
    strategy_id: Mapped[str] = mapped_column(String(48))
    strategy_version: Mapped[int] = mapped_column(Integer)
    timeframe: Mapped[str] = mapped_column(String(16))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    reference_entry: Mapped[Decimal]
    invalidation_level: Mapped[Decimal | None]
    target_reference: Mapped[Decimal | None]
    signal_strength: Mapped[Decimal]
    expires_at: Mapped[datetime]
    risk_result: Mapped[str | None] = mapped_column(String(32))
    permitted_quantity: Mapped[Decimal | None]
    binding_constraint: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(24))
    order_seq: Mapped[int | None] = mapped_column(Integer)
    trade_seq: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestRejectionRecord(Base):
    """Why a draft, a proposal or an entry did not become a trade."""

    __tablename__ = "backtest_rejections"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime]
    stage: Mapped[str] = mapped_column(String(32))
    proposal_seq: Mapped[int | None] = mapped_column(Integer)
    strategy_id: Mapped[str] = mapped_column(String(48))
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str | None] = mapped_column(String(8))
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)


class BacktestOrderRecord(Base):
    """A simulated order with its final state and full event history."""

    __tablename__ = "backtest_orders"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    trade_seq: Mapped[int | None] = mapped_column(Integer)
    proposal_seq: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal]
    filled_quantity: Mapped[Decimal]
    average_price: Mapped[Decimal | None]
    status: Mapped[str] = mapped_column(String(24))
    arrival_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    commission: Mapped[Decimal]
    fees: Mapped[Decimal]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestFillRecord(Base):
    """A simulated fill (7B fill model) and the journal entry that posted it."""

    __tablename__ = "backtest_fills"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_seq: Mapped[int] = mapped_column(Integer)
    trade_seq: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))
    session: Mapped[date] = mapped_column(Date)
    filled_at: Mapped[datetime]
    known_at: Mapped[datetime]
    quantity: Mapped[Decimal]
    price: Mapped[Decimal]
    notional: Mapped[Decimal]
    commission: Mapped[Decimal]
    fees: Mapped[Decimal]
    basis: Mapped[str] = mapped_column(String(24))
    journal_sequence: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "order_seq"],
            ["backtest_orders.run_id", "backtest_orders.seq"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("quantity > 0 AND price > 0", name="positive_fill"),
    )


class BacktestTradeRecord(Base):
    """A simulated trade: one entry order's position, from first fill to exit."""

    __tablename__ = "backtest_trades"
    __table_args__ = (Index(None, "run_id", "strategy_id"),)

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_seq: Mapped[int] = mapped_column(Integer)
    strategy_id: Mapped[str] = mapped_column(String(48))
    timeframe: Mapped[str] = mapped_column(String(16))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    entry_quantity: Mapped[Decimal]
    entry_price: Mapped[Decimal | None]
    exit_quantity: Mapped[Decimal]
    exit_price: Mapped[Decimal | None]
    exit_reason: Mapped[str | None] = mapped_column(String(24))
    realized_gross: Mapped[Decimal]
    unrealized: Mapped[Decimal]
    costs: Mapped[Decimal]
    net_pnl: Mapped[Decimal]
    holding_sessions: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestEquitySnapshotRecord(Base):
    """The simulated account at one session's valuation."""

    __tablename__ = "backtest_equity_snapshots"

    run_id: Mapped[uuid.UUID] = _run_fk()
    session: Mapped[date] = mapped_column(Date, primary_key=True)
    at: Mapped[datetime]
    equity: Mapped[Decimal]
    gross_equity: Mapped[Decimal]
    cash: Mapped[Decimal]
    long_exposure: Mapped[Decimal]
    short_exposure: Mapped[Decimal]
    realized_pnl: Mapped[Decimal]
    unrealized_pnl: Mapped[Decimal]
    total_costs: Mapped[Decimal]
    drawdown: Mapped[Decimal]
    drawdown_pct: Mapped[Decimal]
    open_positions: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestMetricRecord(Base):
    __tablename__ = "backtest_metrics"

    run_id: Mapped[uuid.UUID] = _run_fk()
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[Decimal | None]
    unit: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    sample_size: Mapped[int] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text)


class BacktestBenchmarkResultRecord(Base):
    __tablename__ = "backtest_benchmark_results"

    run_id: Mapped[uuid.UUID] = _run_fk()
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    available: Mapped[bool] = mapped_column(Boolean)
    total_return: Mapped[Decimal | None]
    annualized_return: Mapped[Decimal | None]
    max_drawdown_pct: Mapped[Decimal | None]
    excess_return: Mapped[Decimal | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestRunEventRecord(Base):
    """Notable replay events: breaker trips and resets, splits, universe changes."""

    __tablename__ = "backtest_run_events"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(24))
    symbol: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestRunErrorRecord(Base):
    """Problems: non-fatal (a strategy raised; the run continued) and the fatal one."""

    __tablename__ = "backtest_run_errors"

    run_id: Mapped[uuid.UUID] = _run_fk()
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime | None]
    stage: Mapped[str] = mapped_column(String(32))
    fatal: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BacktestJournalEntryRecord(Base):
    """The simulated account's journal (6A templates), for audit and re-reconciliation."""

    __tablename__ = "backtest_journal_entries"

    run_id: Mapped[uuid.UUID] = _run_fk()
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_at: Mapped[datetime]
    template: Mapped[str] = mapped_column(String(32))
    entry_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    movements: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)


class BacktestEvaluationCountRecord(Base):
    """How many strategy evaluations ended in each signal state."""

    __tablename__ = "backtest_evaluation_counts"

    run_id: Mapped[uuid.UUID] = _run_fk()
    strategy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(16), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


RESULT_TABLES = (
    "backtest_strategy_versions",
    "backtest_event_cursors",
    "backtest_proposals",
    "backtest_rejections",
    "backtest_orders",
    "backtest_fills",
    "backtest_trades",
    "backtest_equity_snapshots",
    "backtest_metrics",
    "backtest_benchmark_results",
    "backtest_run_events",
    "backtest_run_errors",
    "backtest_journal_entries",
    "backtest_evaluation_counts",
)

__all__ = [
    "RESULT_TABLES",
    "BacktestBenchmarkResultRecord",
    "BacktestEquitySnapshotRecord",
    "BacktestEvaluationCountRecord",
    "BacktestEventCursorRecord",
    "BacktestFillRecord",
    "BacktestJournalEntryRecord",
    "BacktestMetricRecord",
    "BacktestOrderRecord",
    "BacktestProposalRecord",
    "BacktestRejectionRecord",
    "BacktestRunErrorRecord",
    "BacktestRunEventRecord",
    "BacktestRunRecord",
    "BacktestStrategyVersionRecord",
    "BacktestTradeRecord",
]

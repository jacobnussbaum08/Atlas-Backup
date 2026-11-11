"""Phase 8A: event-driven historical backtesting engine.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-11

**Simulations over stored historical data only.** Nothing here is an order, a fill or a
journal entry of the paper account; the backtester never writes to a live trading table.

* ``backtest_runs``: the request, the fully resolved configuration (strategy versions, risk
  configuration, execution model, dataset knowledge time, policies, seed, code version),
  status and progress. Status moves only QUEUED -> RUNNING -> COMPLETED | FAILED,
  configuration columns never change, a final run never changes, nothing is deleted
  (``atlas_backtest_run_guard``).
* Result tables (strategy versions, event cursors, proposals, rejections, orders, fills,
  trades, equity snapshots, metrics, benchmark results, run events, run errors, journal
  entries, evaluation counts) are insert-only, and only while their run is RUNNING
  (strategy versions: while QUEUED) (``atlas_backtest_result_guard``), so a completed
  run's records are immutable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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

RUN_GUARD = """
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'backtest runs are never deleted' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('COMPLETED', 'FAILED') THEN
        RAISE EXCEPTION 'backtest run % is % and immutable', OLD.id, OLD.status
            USING ERRCODE = '55000';
    END IF;
    IF (NEW.id, NEW.name, NEW.simulated, NEW.request, NEW.config, NEW.config_sha256,
        NEW.engine_version, NEW.code_version, NEW.seed, NEW.start_date, NEW.end_date,
        NEW.initial_capital, NEW.currency, NEW.universe_mode, NEW.benchmark_symbol,
        NEW.data_provider, NEW.data_feed, NEW.data_known_at, NEW.execution_model_id,
        NEW.execution_model_sha256, NEW.risk_sha256, NEW.risk_source, NEW.reproduction_of,
        NEW.requested_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.name, OLD.simulated, OLD.request, OLD.config, OLD.config_sha256,
        OLD.engine_version, OLD.code_version, OLD.seed, OLD.start_date, OLD.end_date,
        OLD.initial_capital, OLD.currency, OLD.universe_mode, OLD.benchmark_symbol,
        OLD.data_provider, OLD.data_feed, OLD.data_known_at, OLD.execution_model_id,
        OLD.execution_model_sha256, OLD.risk_sha256, OLD.risk_source, OLD.reproduction_of,
        OLD.requested_by, OLD.created_at) THEN
        RAISE EXCEPTION 'the configuration of backtest run % cannot change', OLD.id
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'QUEUED' AND NEW.status IN ('RUNNING', 'FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'backtest run % cannot go from % to %', OLD.id, OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    IF NEW.sessions_done < OLD.sessions_done THEN
        RAISE EXCEPTION 'backtest progress never goes back' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""

RESULT_GUARD = """
DECLARE
    run_status text;
BEGIN
    SELECT status INTO run_status FROM backtest_runs WHERE id = NEW.run_id FOR SHARE;
    IF run_status IS NULL THEN
        RAISE EXCEPTION 'backtest run % does not exist', NEW.run_id USING ERRCODE = '23503';
    END IF;
    IF TG_TABLE_NAME = 'backtest_strategy_versions' THEN
        IF run_status <> 'QUEUED' THEN
            RAISE EXCEPTION 'strategy versions are recorded when backtest run % is created',
                NEW.run_id USING ERRCODE = '55000';
        END IF;
    ELSIF run_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'results of backtest run % are written only while it runs (it is %)',
            NEW.run_id, run_status USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""


def _guards() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION atlas_backtest_run_guard() RETURNS trigger "
        f"LANGUAGE plpgsql AS $${RUN_GUARD}$$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION atlas_backtest_result_guard() RETURNS trigger "
        f"LANGUAGE plpgsql AS $${RESULT_GUARD}$$;"
    )
    op.execute(
        "CREATE TRIGGER backtest_runs_guard BEFORE UPDATE OR DELETE ON backtest_runs "
        "FOR EACH ROW EXECUTE FUNCTION atlas_backtest_run_guard()"
    )
    op.execute(
        "CREATE TRIGGER backtest_runs_no_truncate BEFORE TRUNCATE ON backtest_runs "
        "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
    )
    for table in RESULT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_while_running BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_backtest_result_guard()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "backtest_runs",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="backtest_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=16), nullable=False),
        sa.Column("code_version", sa.String(length=80), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("universe_mode", sa.String(length=16), nullable=False),
        sa.Column("benchmark_symbol", sa.String(length=64), nullable=True),
        sa.Column("data_provider", sa.String(length=32), nullable=False),
        sa.Column("data_feed", sa.String(length=32), nullable=False),
        sa.Column("data_known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_model_id", sa.Integer(), nullable=False),
        sa.Column("execution_model_sha256", sa.String(length=64), nullable=False),
        sa.Column("risk_sha256", sa.String(length=64), nullable=False),
        sa.Column("risk_source", sa.String(length=80), nullable=False),
        sa.Column("reproduction_of", sa.UUID(), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sessions_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sessions_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column("simulated_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=True),
        sa.Column("dataset_bars", sa.Integer(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("reproduction_matches", sa.Boolean(), nullable=True),
        sa.Column("error_stage", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(status IN ('COMPLETED')) = (result_sha256 IS NOT NULL)",
            name=op.f("ck_backtest_runs_result_when_completed"),
        ),
        sa.CheckConstraint(
            "config_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_backtest_runs_config_sha_format")
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name=op.f("ck_backtest_runs_failed_has_error"),
        ),
        sa.CheckConstraint("end_date >= start_date", name=op.f("ck_backtest_runs_date_order")),
        sa.CheckConstraint("initial_capital > 0", name=op.f("ck_backtest_runs_capital_positive")),
        sa.CheckConstraint(
            "sessions_done <= sessions_total", name=op.f("ck_backtest_runs_progress_bounded")
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_backtest_runs_always_simulated")),
        sa.ForeignKeyConstraint(
            ["execution_model_id"],
            ["execution_models.id"],
            name=op.f("fk_backtest_runs_execution_model_id_execution_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reproduction_of"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_runs_reproduction_of_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_runs")),
    )
    op.create_index(
        op.f("ix_backtest_runs_created_at"), "backtest_runs", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_backtest_runs_status"), "backtest_runs", ["status"], unique=False)
    op.create_table(
        "backtest_benchmark_results",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("total_return", sa.Numeric(), nullable=True),
        sa.Column("annualized_return", sa.Numeric(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(), nullable=True),
        sa.Column("excess_return", sa.Numeric(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_benchmark_results_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "kind", name=op.f("pk_backtest_benchmark_results")),
    )
    op.create_table(
        "backtest_equity_snapshots",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("session", sa.Date(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Numeric(), nullable=False),
        sa.Column("gross_equity", sa.Numeric(), nullable=False),
        sa.Column("cash", sa.Numeric(), nullable=False),
        sa.Column("long_exposure", sa.Numeric(), nullable=False),
        sa.Column("short_exposure", sa.Numeric(), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(), nullable=False),
        sa.Column("total_costs", sa.Numeric(), nullable=False),
        sa.Column("drawdown", sa.Numeric(), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(), nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_equity_snapshots_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "session", name=op.f("pk_backtest_equity_snapshots")),
    )
    op.create_table(
        "backtest_evaluation_counts",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_evaluation_counts_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "strategy_id",
            "timeframe",
            "state",
            name=op.f("pk_backtest_evaluation_counts"),
        ),
    )
    op.create_table(
        "backtest_event_cursors",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("session", sa.Date(), nullable=False),
        sa.Column("simulated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sessions_done", sa.Integer(), nullable=False),
        sa.Column("sessions_total", sa.Integer(), nullable=False),
        sa.Column("evaluations", sa.Integer(), nullable=False),
        sa.Column("proposals", sa.Integer(), nullable=False),
        sa.Column("fills", sa.Integer(), nullable=False),
        sa.Column("journal_entries", sa.Integer(), nullable=False),
        sa.Column("reconciliation", sa.String(length=16), nullable=False),
        sa.Column("equity", sa.Numeric(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_event_cursors_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_event_cursors")),
    )
    op.create_table(
        "backtest_journal_entries",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("template", sa.String(length=32), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("movements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_journal_entries_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "sequence", name=op.f("pk_backtest_journal_entries")),
    )
    op.create_table(
        "backtest_metrics",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_metrics_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "scope", "scope_key", "metric", name=op.f("pk_backtest_metrics")
        ),
    )
    op.create_table(
        "backtest_orders",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("trade_seq", sa.Integer(), nullable=True),
        sa.Column("proposal_seq", sa.Integer(), nullable=True),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(), nullable=False),
        sa.Column("average_price", sa.Numeric(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("arrival_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commission", sa.Numeric(), nullable=False),
        sa.Column("fees", sa.Numeric(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_backtest_orders_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_orders_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_orders")),
    )
    op.create_table(
        "backtest_proposals",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session", sa.Date(), nullable=False),
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("reference_entry", sa.Numeric(), nullable=False),
        sa.Column("invalidation_level", sa.Numeric(), nullable=True),
        sa.Column("target_reference", sa.Numeric(), nullable=True),
        sa.Column("signal_strength", sa.Numeric(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_result", sa.String(length=32), nullable=True),
        sa.Column("permitted_quantity", sa.Numeric(), nullable=True),
        sa.Column("binding_constraint", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("order_seq", sa.Integer(), nullable=True),
        sa.Column("trade_seq", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_backtest_proposals_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_proposals_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_proposals")),
    )
    op.create_index(
        op.f("ix_backtest_proposals_run_id_strategy_id"),
        "backtest_proposals",
        ["run_id", "strategy_id"],
        unique=False,
    )
    op.create_table(
        "backtest_rejections",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("proposal_seq", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_rejections_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_rejections")),
    )
    op.create_table(
        "backtest_run_errors",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("fatal", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_run_errors_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_run_errors")),
    )
    op.create_table(
        "backtest_run_events",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_run_events_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_run_events")),
    )
    op.create_table(
        "backtest_trades",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("proposal_seq", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_quantity", sa.Numeric(), nullable=False),
        sa.Column("entry_price", sa.Numeric(), nullable=True),
        sa.Column("exit_quantity", sa.Numeric(), nullable=False),
        sa.Column("exit_price", sa.Numeric(), nullable=True),
        sa.Column("exit_reason", sa.String(length=24), nullable=True),
        sa.Column("realized_gross", sa.Numeric(), nullable=False),
        sa.Column("unrealized", sa.Numeric(), nullable=False),
        sa.Column("costs", sa.Numeric(), nullable=False),
        sa.Column("net_pnl", sa.Numeric(), nullable=False),
        sa.Column("holding_sessions", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_backtest_trades_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_trades_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_trades")),
    )
    op.create_index(
        op.f("ix_backtest_trades_run_id_strategy_id"),
        "backtest_trades",
        ["run_id", "strategy_id"],
        unique=False,
    )
    op.create_table(
        "backtest_fills",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("order_seq", sa.Integer(), nullable=False),
        sa.Column("trade_seq", sa.Integer(), nullable=True),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("session", sa.Date(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("notional", sa.Numeric(), nullable=False),
        sa.Column("commission", sa.Numeric(), nullable=False),
        sa.Column("fees", sa.Numeric(), nullable=False),
        sa.Column("basis", sa.String(length=24), nullable=False),
        sa.Column("journal_sequence", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0", name=op.f("ck_backtest_fills_positive_fill")
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_backtest_fills_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "order_seq"],
            ["backtest_orders.run_id", "backtest_orders.seq"],
            name=op.f("fk_backtest_fills_run_id_backtest_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_fills_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_backtest_fills")),
    )
    op.create_table(
        "backtest_strategy_versions",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("logic_version", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("parameters_source", sa.String(length=32), nullable=False),
        sa.Column("timeframes", sa.ARRAY(sa.String(length=16)), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_strategy_versions_run_id_backtest_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name=op.f("fk_backtest_strategy_versions_strategy_id_strategies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_backtest_strategy_versions_strategy_version_id_strategy_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "strategy_id", name=op.f("pk_backtest_strategy_versions")
        ),
    )
    # ### end Alembic commands ###
    _guards()


def downgrade() -> None:
    for table in RESULT_TABLES:
        for suffix in ("while_running", "append_only", "no_truncate"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix} ON {table}")
    op.execute("DROP TRIGGER IF EXISTS backtest_runs_guard ON backtest_runs")
    op.execute("DROP TRIGGER IF EXISTS backtest_runs_no_truncate ON backtest_runs")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("backtest_strategy_versions")
    op.drop_table("backtest_fills")
    op.drop_index(op.f("ix_backtest_trades_run_id_strategy_id"), table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_table("backtest_run_events")
    op.drop_table("backtest_run_errors")
    op.drop_table("backtest_rejections")
    op.drop_index(op.f("ix_backtest_proposals_run_id_strategy_id"), table_name="backtest_proposals")
    op.drop_table("backtest_proposals")
    op.drop_table("backtest_orders")
    op.drop_table("backtest_metrics")
    op.drop_table("backtest_journal_entries")
    op.drop_table("backtest_event_cursors")
    op.drop_table("backtest_evaluation_counts")
    op.drop_table("backtest_equity_snapshots")
    op.drop_table("backtest_benchmark_results")
    op.drop_index(op.f("ix_backtest_runs_status"), table_name="backtest_runs")
    op.drop_index(op.f("ix_backtest_runs_created_at"), table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.execute("DROP FUNCTION IF EXISTS atlas_backtest_result_guard()")
    op.execute("DROP FUNCTION IF EXISTS atlas_backtest_run_guard()")
    # ### end Alembic commands ###

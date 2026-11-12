"""Phase 10A: autonomous paper-trading orchestration.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-11

**Paper trading only.** Nothing here is an order, a fill or a journal entry; these tables
record how the orchestrator sequenced the existing engines.

* ``trading_control_events``: the operator's trading mode per paper account. Append-only,
  numbered gaplessly per account, each event starting from the previous one's mode
  (``atlas_trading_control_guard``); operators only (CHECK). No row means DISABLED.
* ``trade_intents``: the inbox of prioritized AI decisions, UNIQUE per (account, decision).
  Never deleted; its status moves once from RECEIVED to a final outcome and a final row
  never changes (``atlas_trade_intent_guard``).
* ``orchestration_jobs``: scheduled jobs, UNIQUE per (kind, slot); never deleted.
* ``orchestrator_status``: the orchestrator's heartbeat row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODES = ("DISABLED", "ENABLED", "PAUSED", "EMERGENCY_STOP")

CONTROL_GUARD = """
DECLARE
    previous trading_control_events%ROWTYPE;
BEGIN
    SELECT * INTO previous FROM trading_control_events
     WHERE portfolio_account_id = NEW.portfolio_account_id
     ORDER BY sequence DESC LIMIT 1;
    IF previous.id IS NULL THEN
        IF NEW.sequence <> 1 OR NEW.from_mode <> 'DISABLED' THEN
            RAISE EXCEPTION 'the first trading control event is number 1 and starts from DISABLED'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.sequence <> previous.sequence + 1 OR NEW.from_mode <> previous.to_mode THEN
        RAISE EXCEPTION 'trading control event % must follow % and start from %',
            NEW.sequence, previous.sequence, previous.to_mode USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
"""

INTENT_GUARD = """
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'trade intents are never deleted' USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'RECEIVED' THEN
        RAISE EXCEPTION 'trade intent % is final (%) and never changes', OLD.id, OLD.status
            USING ERRCODE = '55000';
    END IF;
    IF NEW.portfolio_account_id <> OLD.portfolio_account_id
       OR NEW.ai_decision_id <> OLD.ai_decision_id
       OR NEW.decision_run_id <> OLD.decision_run_id
       OR NEW.received_at <> OLD.received_at
       OR NEW.decision_expires_at <> OLD.decision_expires_at THEN
        RAISE EXCEPTION 'the identity of a trade intent never changes' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""

NO_DELETE = """
BEGIN
    RAISE EXCEPTION '% rows are never deleted', TG_TABLE_NAME USING ERRCODE = '55000';
END;
"""


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "orchestration_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "kind",
            _enum(
                "orchestration_job_kind",
                "RISK_MONITOR",
                "RESERVATION_RECONCILE",
                "EXECUTION_RECONCILE",
                "ACCOUNTING_RECONCILE",
                "WORKER_HEALTH",
                "MARKET_INTELLIGENCE",
                "DECISION_OUTCOMES",
            ),
            nullable=False,
        ),
        sa.Column("slot", sa.String(length=64), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            _enum("orchestration_job_status", "RUNNING", "SUCCEEDED", "FAILED", "DEAD"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=96), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reclaimed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> 'RUNNING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_orchestration_jobs_running_has_lease"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('SUCCEEDED', 'DEAD') OR finished_at IS NOT NULL",
            name=op.f("ck_orchestration_jobs_final_has_finish"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_orchestration_jobs_attempts_valid")),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_orchestration_jobs_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestration_jobs")),
        sa.UniqueConstraint("kind", "slot", name=op.f("uq_orchestration_jobs_kind_slot")),
    )
    op.create_index(
        op.f("ix_orchestration_jobs_kind_created_at"),
        "orchestration_jobs",
        ["kind", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_orchestration_jobs_status_next_attempt_at"),
        "orchestration_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_table(
        "orchestrator_status",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=96), nullable=True),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "state",
            _enum("orchestrator_state", "STARTING", "RUNNING", "DEGRADED", "STOPPED"),
            nullable=False,
        ),
        sa.Column("trading_mode", _enum("orchestrator_trading_mode", *MODES), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tick_ms", sa.Integer(), nullable=True),
        sa.Column(
            "counters", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_orchestrator_status_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_orchestrator_status")),
    )
    op.create_table(
        "trading_control_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            _enum(
                "trading_control_action",
                "ENABLE",
                "DISABLE",
                "PAUSE",
                "RESUME",
                "EMERGENCY_STOP",
                "CLEAR_EMERGENCY",
            ),
            nullable=False,
        ),
        sa.Column("from_mode", _enum("trading_mode_from", *MODES), nullable=False),
        sa.Column("to_mode", _enum("trading_mode_to", *MODES), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor ~ '^operator:[A-Za-z0-9_.@-]+$'",
            name=op.f("ck_trading_control_events_operator_only"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_trading_control_events_reason_given")
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_trading_control_events_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_trading_control_events_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trading_control_events")),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "sequence",
            name=op.f("uq_trading_control_events_portfolio_account_id_sequence"),
        ),
    )
    op.create_table(
        "trade_intents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("ai_decision_id", sa.UUID(), nullable=False),
        sa.Column("decision_run_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("decision_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            _enum(
                "trade_intent_status",
                "RECEIVED",
                "ORDER_CREATED",
                "RISK_REJECTED",
                "EXECUTION_REFUSED",
                "NOT_TRADED",
                "INVALID",
                "DEAD",
            ),
            nullable=False,
        ),
        sa.Column("trading_mode", _enum("trade_intent_mode", *MODES), nullable=True),
        sa.Column("reservation_id", sa.UUID(), nullable=True),
        sa.Column("reservation_decision", sa.String(length=16), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("execution_request_id", sa.UUID(), nullable=True),
        sa.Column(
            "reasons", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'RECEIVED') = (decided_at IS NULL)",
            name=op.f("ck_trade_intents_decided_iff_final"),
        ),
        sa.CheckConstraint(
            "status <> 'ORDER_CREATED' OR (order_id IS NOT NULL AND reservation_id IS NOT NULL)",
            name=op.f("ck_trade_intents_order_created_has_order"),
        ),
        sa.ForeignKeyConstraint(
            ["ai_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_trade_intents_ai_decision_id_ai_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_trade_intents_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decision_run_id"],
            ["decision_runs.id"],
            name=op.f("fk_trade_intents_decision_run_id_decision_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_request_id"],
            ["execution_requests.id"],
            name=op.f("fk_trade_intents_execution_request_id_execution_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_trade_intents_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_trade_intents_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_trade_intents_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_intents")),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "ai_decision_id",
            name=op.f("uq_trade_intents_portfolio_account_id_ai_decision_id"),
        ),
    )
    op.create_index(
        op.f("ix_trade_intents_portfolio_account_id_received_at"),
        "trade_intents",
        ["portfolio_account_id", "received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_trade_intents_status_received_at"),
        "trade_intents",
        ["status", "received_at"],
        unique=False,
    )

    # ---------------------------------------------------------------- guards
    op.execute(
        "CREATE FUNCTION atlas_trading_control_guard() RETURNS trigger LANGUAGE plpgsql AS $$"
        + CONTROL_GUARD
        + "$$"
    )
    op.execute(
        "CREATE TRIGGER trading_control_events_sequence BEFORE INSERT ON trading_control_events "
        "FOR EACH ROW EXECUTE FUNCTION atlas_trading_control_guard()"
    )
    op.execute(
        "CREATE TRIGGER trading_control_events_append_only BEFORE UPDATE OR DELETE ON "
        "trading_control_events FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
    )
    op.execute(
        "CREATE FUNCTION atlas_trade_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$"
        + INTENT_GUARD
        + "$$"
    )
    op.execute(
        "CREATE TRIGGER trade_intents_guard BEFORE UPDATE OR DELETE ON trade_intents "
        "FOR EACH ROW EXECUTE FUNCTION atlas_trade_intent_guard()"
    )
    op.execute(
        "CREATE FUNCTION atlas_reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$"
        + NO_DELETE
        + "$$"
    )
    op.execute(
        "CREATE TRIGGER orchestration_jobs_no_delete BEFORE DELETE ON orchestration_jobs "
        "FOR EACH ROW EXECUTE FUNCTION atlas_reject_delete()"
    )
    for table in ("trading_control_events", "trade_intents", "orchestration_jobs"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )


def downgrade() -> None:
    for table in ("trading_control_events", "trade_intents", "orchestration_jobs"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute("DROP TRIGGER IF EXISTS orchestration_jobs_no_delete ON orchestration_jobs")
    op.execute("DROP TRIGGER IF EXISTS trade_intents_guard ON trade_intents")
    op.execute(
        "DROP TRIGGER IF EXISTS trading_control_events_append_only ON trading_control_events"
    )
    op.execute("DROP TRIGGER IF EXISTS trading_control_events_sequence ON trading_control_events")
    op.execute("DROP FUNCTION IF EXISTS atlas_reject_delete()")
    op.execute("DROP FUNCTION IF EXISTS atlas_trade_intent_guard()")
    op.execute("DROP FUNCTION IF EXISTS atlas_trading_control_guard()")
    op.drop_index(op.f("ix_trade_intents_status_received_at"), table_name="trade_intents")
    op.drop_index(
        op.f("ix_trade_intents_portfolio_account_id_received_at"), table_name="trade_intents"
    )
    op.drop_table("trade_intents")
    op.drop_table("trading_control_events")
    op.drop_table("orchestrator_status")
    op.drop_index(
        op.f("ix_orchestration_jobs_status_next_attempt_at"), table_name="orchestration_jobs"
    )
    op.drop_index(op.f("ix_orchestration_jobs_kind_created_at"), table_name="orchestration_jobs")
    op.drop_table("orchestration_jobs")

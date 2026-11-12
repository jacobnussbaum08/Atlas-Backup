"""Phase 10B: reliability and sustained operation.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-11

**Paper trading only.** Additive: no existing row is changed or removed.

* ``worker_heartbeats``: per-process heartbeat rows (the position monitor's liveness).
* ``operator_credentials``: named operator tokens (SHA-256 digests only). Never deleted;
  the name and the digest never change (``atlas_operator_credential_guard``).
* ``alert_acknowledgements``: append-only acknowledgements of ``system_health_events``.
* ``orchestration_jobs``: new kinds (DATA_FRESHNESS, SESSION_OPEN_CHECK,
  SESSION_CLOSE_REVIEW, DEGRADATION_MONITOR), the SUPERSEDED status and the requeue columns.
* ``orchestrator_status``: the entry protection gate of the last tick.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_0020 = (
    "RISK_MONITOR",
    "RESERVATION_RECONCILE",
    "EXECUTION_RECONCILE",
    "ACCOUNTING_RECONCILE",
    "WORKER_HEALTH",
    "MARKET_INTELLIGENCE",
    "DECISION_OUTCOMES",
)
KINDS_0021 = (
    *KINDS_0020,
    "DATA_FRESHNESS",
    "SESSION_OPEN_CHECK",
    "SESSION_CLOSE_REVIEW",
    "DEGRADATION_MONITOR",
)
STATUSES_0020 = ("RUNNING", "SUCCEEDED", "FAILED", "DEAD")
STATUSES_0021 = (*STATUSES_0020, "SUPERSEDED")

CREDENTIAL_GUARD = """
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'operator credentials are never deleted; revoke them'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.name <> OLD.name OR NEW.token_sha256 <> OLD.token_sha256
       OR NEW.created_at <> OLD.created_at OR NEW.created_by <> OLD.created_by THEN
        RAISE EXCEPTION 'the identity of an operator credential never changes'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
       OR NEW.can_control_trading <> OLD.can_control_trading) THEN
        RAISE EXCEPTION 'a revoked operator credential stays revoked' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _swap_check(table: str, name: str, column: str, values: Sequence[str]) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    allowed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(op.f(name), table, f"{column} IN ({allowed})")


def upgrade() -> None:
    # ---------------------------------------------------------------- jobs
    op.alter_column(
        "orchestration_jobs",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=10),  # SUPERSEDED
        existing_nullable=False,
    )
    _swap_check(
        "orchestration_jobs", "ck_orchestration_jobs_orchestration_job_kind", "kind", KINDS_0021
    )
    _swap_check(
        "orchestration_jobs",
        "ck_orchestration_jobs_orchestration_job_status",
        "status",
        STATUSES_0021,
    )
    op.drop_constraint(
        op.f("ck_orchestration_jobs_final_has_finish"), "orchestration_jobs", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_orchestration_jobs_final_has_finish"),
        "orchestration_jobs",
        "status NOT IN ('SUCCEEDED', 'DEAD', 'SUPERSEDED') OR finished_at IS NOT NULL",
    )
    op.add_column(
        "orchestration_jobs",
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orchestration_jobs", sa.Column("requeued_by", sa.String(length=128), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_orchestration_jobs_requeue_has_operator"),
        "orchestration_jobs",
        "(requeued_at IS NULL) = (requeued_by IS NULL)",
    )

    # ---------------------------------------------------------------- orchestrator status
    op.add_column("orchestrator_status", sa.Column("entry_gate_open", sa.Boolean(), nullable=True))
    op.add_column(
        "orchestrator_status",
        sa.Column(
            "entry_gate_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )

    # ---------------------------------------------------------------- worker heartbeats
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=96), nullable=False),
        sa.Column(
            "state",
            _enum("worker_heartbeat_state", "STARTING", "RUNNING", "DEGRADED", "STOPPED"),
            nullable=False,
        ),
        sa.Column("interval_seconds", sa.Numeric(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("passes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker", "instance_id", name=op.f("pk_worker_heartbeats")),
    )
    op.create_index(
        op.f("ix_worker_heartbeats_worker_heartbeat_at"),
        "worker_heartbeats",
        ["worker", "heartbeat_at"],
        unique=False,
    )

    # ---------------------------------------------------------------- operators
    op.create_table(
        "operator_credentials",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "can_control_trading", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=128), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9_.@-]{2,64}$'", name=op.f("ck_operator_credentials_name_valid")
        ),
        sa.CheckConstraint(
            "token_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_operator_credentials_digest_valid")
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name=op.f("ck_operator_credentials_revocation_has_operator"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_operator_credentials_reason_given")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_credentials")),
        sa.UniqueConstraint("name", name=op.f("uq_operator_credentials_name")),
        sa.UniqueConstraint("token_sha256", name=op.f("uq_operator_credentials_token_sha256")),
    )
    op.execute(
        "CREATE FUNCTION atlas_operator_credential_guard() RETURNS trigger LANGUAGE plpgsql "
        "AS $$" + CREDENTIAL_GUARD + "$$"
    )
    op.execute(
        "CREATE TRIGGER operator_credentials_guard BEFORE UPDATE OR DELETE ON "
        "operator_credentials FOR EACH ROW EXECUTE FUNCTION atlas_operator_credential_guard()"
    )

    # ---------------------------------------------------------------- acknowledgements
    op.create_table(
        "alert_acknowledgements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("health_event_id", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor ~ '^operator:[A-Za-z0-9_.@-]+$'",
            name=op.f("ck_alert_acknowledgements_operator_only"),
        ),
        sa.CheckConstraint(
            "length(btrim(note)) >= 3", name=op.f("ck_alert_acknowledgements_note_given")
        ),
        sa.ForeignKeyConstraint(
            ["health_event_id"],
            ["system_health_events.id"],
            name=op.f("fk_alert_acknowledgements_health_event_id_system_health_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_acknowledgements")),
    )
    op.create_index(
        op.f("ix_alert_acknowledgements_health_event_id"),
        "alert_acknowledgements",
        ["health_event_id"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER alert_acknowledgements_append_only BEFORE UPDATE OR DELETE ON "
        "alert_acknowledgements FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
    )
    for table in ("operator_credentials", "alert_acknowledgements"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )


def downgrade() -> None:
    for table in ("operator_credentials", "alert_acknowledgements"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS alert_acknowledgements_append_only ON alert_acknowledgements"
    )
    op.drop_index(
        op.f("ix_alert_acknowledgements_health_event_id"), table_name="alert_acknowledgements"
    )
    op.drop_table("alert_acknowledgements")
    op.execute("DROP TRIGGER IF EXISTS operator_credentials_guard ON operator_credentials")
    op.execute("DROP FUNCTION IF EXISTS atlas_operator_credential_guard()")
    op.drop_table("operator_credentials")
    op.drop_index(op.f("ix_worker_heartbeats_worker_heartbeat_at"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_column("orchestrator_status", "entry_gate_reasons")
    op.drop_column("orchestrator_status", "entry_gate_open")
    op.drop_constraint(
        op.f("ck_orchestration_jobs_requeue_has_operator"), "orchestration_jobs", type_="check"
    )
    op.drop_column("orchestration_jobs", "requeued_by")
    op.drop_column("orchestration_jobs", "requeued_at")
    # Fails if 10B job kinds or SUPERSEDED rows exist: job history is never deleted.
    op.drop_constraint(
        op.f("ck_orchestration_jobs_final_has_finish"), "orchestration_jobs", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_orchestration_jobs_final_has_finish"),
        "orchestration_jobs",
        "status NOT IN ('SUCCEEDED', 'DEAD') OR finished_at IS NOT NULL",
    )
    _swap_check(
        "orchestration_jobs",
        "ck_orchestration_jobs_orchestration_job_status",
        "status",
        STATUSES_0020,
    )
    _swap_check(
        "orchestration_jobs", "ck_orchestration_jobs_orchestration_job_kind", "kind", KINDS_0020
    )
    op.alter_column(
        "orchestration_jobs",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=9),
        existing_nullable=False,
    )

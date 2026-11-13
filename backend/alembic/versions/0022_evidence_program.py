"""Phase 11A: evidence program (pre-registered protocols, data audits, scorecards).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-12

**Paper trading only.** Additive: no existing row is changed or removed.

* ``evidence_protocols``: pre-registered promotion and rejection criteria, operator-only,
  one row per (name, version), unique criteria digest.
* ``evidence_data_audits``: stored audits of historical datasets.
* ``evidence_scorecards`` and ``evidence_scorecard_entries``: readiness scorecards with one
  classification per strategy and for the portfolio.

All four tables are append-only (UPDATE, DELETE and TRUNCATE refused by
``atlas_reject_modification``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "evidence_protocols",
    "evidence_data_audits",
    "evidence_scorecards",
    "evidence_scorecard_entries",
)
CLASSIFICATIONS = (
    "REJECTED",
    "INSUFFICIENT_EVIDENCE",
    "NOT_DEMONSTRATED",
    "CONTINUE_PAPER",
    "CANDIDATE_FOR_REVIEW",
)


def _check(name: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(name))


def _jsonb(name: str, default: str | None = None) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=default,
        nullable=False,
    )


def _created() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "evidence_protocols",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _jsonb("criteria"),
        sa.Column("criteria_sha256", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("registered_by", sa.String(length=128), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
        sa.CheckConstraint(
            "criteria_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evidence_protocols_criteria_sha_format"),
        ),
        sa.CheckConstraint(
            "registered_by ~ '^operator:[A-Za-z0-9_.@-]+$'",
            name=op.f("ck_evidence_protocols_operator_only"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_evidence_protocols_version_positive")),
        sa.CheckConstraint(
            "length(btrim(rationale)) >= 3", name=op.f("ck_evidence_protocols_rationale_given")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_protocols")),
        sa.UniqueConstraint("criteria_sha256", name=op.f("uq_evidence_protocols_criteria_sha256")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_evidence_protocols_name_version")),
    )
    op.create_table(
        "evidence_data_audits",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        _jsonb("scope"),
        sa.Column("status", sa.String(length=4), nullable=False),
        _jsonb("result"),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
        _check("ck_evidence_data_audits_evidence_audit_status", "status", ("PASS", "WARN", "FAIL")),
        sa.CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evidence_data_audits_result_sha_format"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_data_audits")),
    )
    op.create_index(
        op.f("ix_evidence_data_audits_created_at"), "evidence_data_audits", ["created_at"]
    )
    op.create_table(
        "evidence_scorecards",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("protocol_id", sa.UUID(), nullable=False),
        sa.Column("data_audit_id", sa.UUID(), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        _jsonb("inputs"),
        _jsonb("result"),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _created(),
        _check(
            "ck_evidence_scorecards_evidence_readiness_verdict",
            "verdict",
            ("NOT_READY", "READY_FOR_REVIEW"),
        ),
        sa.CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evidence_scorecards_result_sha_format"),
        ),
        sa.ForeignKeyConstraint(
            ["protocol_id"],
            ["evidence_protocols.id"],
            name=op.f("fk_evidence_scorecards_protocol_id_evidence_protocols"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["data_audit_id"],
            ["evidence_data_audits.id"],
            name=op.f("fk_evidence_scorecards_data_audit_id_evidence_data_audits"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_scorecards")),
    )
    op.create_index(
        op.f("ix_evidence_scorecards_created_at"), "evidence_scorecards", ["created_at"]
    )
    op.create_table(
        "evidence_scorecard_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("scorecard_id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=21), nullable=False),
        _jsonb("reasons", "[]"),
        _created(),
        _check(
            "ck_evidence_scorecard_entries_evidence_classification",
            "classification",
            CLASSIFICATIONS,
        ),
        sa.ForeignKeyConstraint(
            ["scorecard_id"],
            ["evidence_scorecards.id"],
            name=op.f("fk_evidence_scorecard_entries_scorecard_id_evidence_scorecards"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_scorecard_entries")),
        sa.UniqueConstraint(
            "scorecard_id",
            "subject",
            name=op.f("uq_evidence_scorecard_entries_scorecard_id_subject"),
        ),
    )
    op.create_index(
        op.f("ix_evidence_scorecard_entries_subject_created_at"),
        "evidence_scorecard_entries",
        ["subject", "created_at"],
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(
        op.f("ix_evidence_scorecard_entries_subject_created_at"),
        table_name="evidence_scorecard_entries",
    )
    op.drop_table("evidence_scorecard_entries")
    op.drop_index(op.f("ix_evidence_scorecards_created_at"), table_name="evidence_scorecards")
    op.drop_table("evidence_scorecards")
    op.drop_index(op.f("ix_evidence_data_audits_created_at"), table_name="evidence_data_audits")
    op.drop_table("evidence_data_audits")
    op.drop_table("evidence_protocols")

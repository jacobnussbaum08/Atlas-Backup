"""strategy signals and conflicts (Phase 4B)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03 05:04:24.992117+00:00

* ``strategy_evaluations``: ``signal_state`` (what the evaluation found: SIGNAL,
  NO_SIGNAL, INSUFFICIENT_DATA, MISSING_DATA, UNAVAILABLE_DATA, INVALID_DATA,
  INELIGIBLE_ASSET, DUPLICATE, FAILED), ``signal_reason``, ``checks`` (the conditions the
  strategy evaluated) and ``eligibility`` (point-in-time asset eligibility). Evaluations
  finished before this migration get a state derived from their status and skip reasons;
  the outcome-once guard is suspended only for that backfill.
* ``strategy_proposals``: ``evaluation_event`` (trigger and reference) with one proposal per
  strategy, asset, timeframe, direction, mode, basis and event; ``levels`` (how entry,
  invalidation and target were derived). Existing proposals are backfilled from their
  evaluation, with the append-only trigger suspended only for that backfill.
* ``strategy_proposal_conflicts`` (append-only): overlapping opposite-direction proposals.
* ``feature_definitions.source`` accepts ``BENCHMARK`` (cross-asset features).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNAL_STATES = (
    "SIGNAL",
    "NO_SIGNAL",
    "INSUFFICIENT_DATA",
    "MISSING_DATA",
    "UNAVAILABLE_DATA",
    "INVALID_DATA",
    "INELIGIBLE_ASSET",
    "DUPLICATE",
    "FAILED",
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "strategy_proposal_conflicts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("proposal_a_id", sa.UUID(), nullable=False),
        sa.Column("proposal_b_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "OPPOSING_DIRECTION",
                name="proposal_conflict_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.Enum(
                "LIVE",
                "HISTORICAL",
                name="evaluation_mode",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "basis",
            sa.Enum(
                "RECORDED",
                "RECONSTRUCTED",
                name="knowledge_basis",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("overlap_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overlap_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_by", sa.UUID(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "detected_by = proposal_a_id OR detected_by = proposal_b_id",
            name=op.f("ck_strategy_proposal_conflicts_detected_by_member"),
        ),
        sa.CheckConstraint(
            "overlap_end > overlap_start",
            name=op.f("ck_strategy_proposal_conflicts_overlap_positive"),
        ),
        sa.CheckConstraint(
            "proposal_a_id < proposal_b_id",
            name=op.f("ck_strategy_proposal_conflicts_ordered_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_strategy_proposal_conflicts_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_a_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_strategy_proposal_conflicts_proposal_a_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_b_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_strategy_proposal_conflicts_proposal_b_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_proposal_conflicts")),
        sa.UniqueConstraint(
            "proposal_a_id",
            "proposal_b_id",
            name=op.f("uq_strategy_proposal_conflicts_proposal_a_id_proposal_b_id"),
        ),
    )
    op.create_index(
        op.f("ix_strategy_proposal_conflicts_asset_id_overlap_start"),
        "strategy_proposal_conflicts",
        ["asset_id", "overlap_start"],
        unique=False,
    )
    op.create_index(
        op.f("ix_strategy_proposal_conflicts_proposal_b_id"),
        "strategy_proposal_conflicts",
        ["proposal_b_id"],
        unique=False,
    )
    for statement in (
        "CREATE TRIGGER strategy_proposal_conflicts_append_only BEFORE UPDATE OR DELETE ON "
        "strategy_proposal_conflicts FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()",
        "CREATE TRIGGER strategy_proposal_conflicts_no_truncate BEFORE TRUNCATE ON "
        "strategy_proposal_conflicts FOR EACH STATEMENT EXECUTE FUNCTION "
        "atlas_reject_modification()",
    ):
        op.execute(statement)

    # --- strategy_evaluations -----------------------------------------------------------
    op.add_column(
        "strategy_evaluations",
        sa.Column("signal_state", sa.String(length=17), nullable=True),
    )
    op.add_column("strategy_evaluations", sa.Column("signal_reason", sa.Text(), nullable=True))
    op.add_column(
        "strategy_evaluations",
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "strategy_evaluations",
        sa.Column("eligibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("ALTER TABLE strategy_evaluations DISABLE TRIGGER strategy_evaluations_outcome_once")
    op.execute(
        """
        UPDATE strategy_evaluations e SET signal_state = CASE
            WHEN e.status = 'PROPOSED' THEN 'SIGNAL'
            WHEN e.status = 'NO_PROPOSAL' THEN 'NO_SIGNAL'
            WHEN e.status = 'FAILED' THEN 'FAILED'
            WHEN e.status = 'SKIPPED' THEN CASE
                WHEN e.skip_reasons @> '[{"status": "INSUFFICIENT_DATA"}]' THEN 'INSUFFICIENT_DATA'
                WHEN e.skip_reasons @> '[{"status": "MISSING_INPUTS"}]' THEN 'MISSING_DATA'
                WHEN e.skip_reasons @> '[{"status": "UNAVAILABLE"}]' THEN 'UNAVAILABLE_DATA'
                ELSE 'INVALID_DATA'
            END
        END
        WHERE e.status <> 'RUNNING'
        """
    )
    op.execute("ALTER TABLE strategy_evaluations ENABLE TRIGGER strategy_evaluations_outcome_once")
    op.create_check_constraint(
        op.f("ck_strategy_evaluations_strategy_signal_state"),
        "strategy_evaluations",
        f"signal_state IN ({_in(SIGNAL_STATES)})",
    )
    op.create_check_constraint(
        op.f("ck_strategy_evaluations_signal_state_iff_done"),
        "strategy_evaluations",
        "(status = 'RUNNING') = (signal_state IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_strategy_evaluations_signal_iff_proposed"),
        "strategy_evaluations",
        "(status = 'PROPOSED') = (signal_state = 'SIGNAL')",
    )
    op.create_index(
        op.f("ix_strategy_evaluations_signal_state_started_at"),
        "strategy_evaluations",
        ["signal_state", "started_at"],
        unique=False,
    )

    # --- strategy_proposals ---------------------------------------------------------------
    op.add_column(
        "strategy_proposals",
        sa.Column("evaluation_event", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "strategy_proposals",
        sa.Column(
            "levels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE strategy_proposals DISABLE TRIGGER strategy_proposals_append_only")
    op.execute(
        """
        UPDATE strategy_proposals p SET evaluation_event = e.trigger || ':' || e.trigger_ref
        FROM strategy_evaluations e WHERE e.id = p.evaluation_id
        """
    )
    op.execute("ALTER TABLE strategy_proposals ENABLE TRIGGER strategy_proposals_append_only")
    op.alter_column("strategy_proposals", "evaluation_event", nullable=False)
    op.create_unique_constraint(
        op.f("uq_strategy_proposals_event"),
        "strategy_proposals",
        ["strategy_id", "asset_id", "timeframe", "direction", "mode", "basis", "evaluation_event"],
    )

    # --- feature_definitions.source: BENCHMARK ----------------------------------------------
    op.drop_constraint(
        op.f("ck_feature_definitions_feature_source"), "feature_definitions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_feature_definitions_feature_source"),
        "feature_definitions",
        "source IN ('BARS', 'BENCHMARK', 'MARKET_INTELLIGENCE', 'NEWS')",
    )


def downgrade() -> None:
    # Benchmark feature definitions cannot be removed (append-only registry); the
    # downgrade restores the old CHECK only when none is registered.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM feature_definitions WHERE source = 'BENCHMARK') THEN
                RAISE EXCEPTION 'BENCHMARK feature definitions are registered; downgrade refused';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        op.f("ck_feature_definitions_feature_source"), "feature_definitions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_feature_definitions_feature_source"),
        "feature_definitions",
        "source IN ('BARS', 'MARKET_INTELLIGENCE', 'NEWS')",
    )
    op.drop_constraint(op.f("uq_strategy_proposals_event"), "strategy_proposals", type_="unique")
    op.drop_column("strategy_proposals", "levels")
    op.drop_column("strategy_proposals", "evaluation_event")
    op.drop_index(
        op.f("ix_strategy_evaluations_signal_state_started_at"), table_name="strategy_evaluations"
    )
    for name in (
        op.f("ck_strategy_evaluations_signal_iff_proposed"),
        op.f("ck_strategy_evaluations_signal_state_iff_done"),
        op.f("ck_strategy_evaluations_strategy_signal_state"),
    ):
        op.drop_constraint(name, "strategy_evaluations", type_="check")
    op.drop_column("strategy_evaluations", "eligibility")
    op.drop_column("strategy_evaluations", "checks")
    op.drop_column("strategy_evaluations", "signal_reason")
    op.drop_column("strategy_evaluations", "signal_state")
    op.execute(
        "DROP TRIGGER IF EXISTS strategy_proposal_conflicts_no_truncate ON "
        "strategy_proposal_conflicts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS strategy_proposal_conflicts_append_only ON "
        "strategy_proposal_conflicts"
    )
    op.drop_index(
        op.f("ix_strategy_proposal_conflicts_proposal_b_id"),
        table_name="strategy_proposal_conflicts",
    )
    op.drop_index(
        op.f("ix_strategy_proposal_conflicts_asset_id_overlap_start"),
        table_name="strategy_proposal_conflicts",
    )
    op.drop_table("strategy_proposal_conflicts")

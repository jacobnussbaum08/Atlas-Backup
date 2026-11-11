"""Phase 8B: walk-forward validation, strategy optimization and model learning governance.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-11

**Simulations over stored historical data only.** Nothing here is an order, a fill or a
journal entry of the paper account.

* ``validation_experiments``: experiment configurations; append-only.
* ``validation_runs``: one execution of an experiment with its resolved base configuration
  and its reserved holdout. Status moves only QUEUED -> RUNNING -> COMPLETED | FAILED,
  configuration columns (including the holdout dates) never change, a final run never
  changes, nothing is deleted (``atlas_validation_run_guard``).
* Run results (``validation_trials``, ``validation_folds``, ``validation_robustness_results``,
  ``validation_segment_results``) are insert-only and only while their run is RUNNING
  (``atlas_validation_result_guard``).
* ``validation_holdout_evaluations`` are insert-only and only for a COMPLETED run
  (``atlas_validation_holdout_guard``): the untouched final period is read only after
  selection is frozen, and every read is numbered.
* Comparisons, champion/challenger candidates, their lifecycle events, approval records,
  monitoring rules, drift measurements and degradation alerts are append-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESULT_TABLES = (
    "validation_trials",
    "validation_folds",
    "validation_robustness_results",
    "validation_segment_results",
)
APPEND_ONLY = (
    *RESULT_TABLES,
    "validation_holdout_evaluations",
    "validation_experiments",
    "validation_comparisons",
    "learning_candidates",
    "learning_candidate_events",
    "learning_approvals",
    "learning_monitoring_rules",
    "learning_drift_measurements",
    "learning_degradation_alerts",
)

RUN_GUARD = """
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'validation runs are never deleted' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('COMPLETED', 'FAILED') THEN
        RAISE EXCEPTION 'validation run % is % and immutable', OLD.id, OLD.status
            USING ERRCODE = '55000';
    END IF;
    IF (NEW.id, NEW.experiment_id, NEW.base_config, NEW.base_config_sha256, NEW.engine_version,
        NEW.code_version, NEW.seed, NEW.start_date, NEW.end_date, NEW.data_known_at,
        NEW.requested_by, NEW.reproduction_of, NEW.created_at, NEW.holdout_start,
        NEW.holdout_end)
       IS DISTINCT FROM
       (OLD.id, OLD.experiment_id, OLD.base_config, OLD.base_config_sha256, OLD.engine_version,
        OLD.code_version, OLD.seed, OLD.start_date, OLD.end_date, OLD.data_known_at,
        OLD.requested_by, OLD.reproduction_of, OLD.created_at, OLD.holdout_start,
        OLD.holdout_end) THEN
        RAISE EXCEPTION 'the configuration of validation run % cannot change', OLD.id
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'QUEUED' AND NEW.status IN ('RUNNING', 'FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'validation run % cannot go from % to %', OLD.id, OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""

RESULT_GUARD = """
DECLARE
    run_status text;
BEGIN
    SELECT status INTO run_status FROM validation_runs WHERE id = NEW.run_id FOR SHARE;
    IF run_status IS NULL THEN
        RAISE EXCEPTION 'validation run % does not exist', NEW.run_id USING ERRCODE = '23503';
    END IF;
    IF run_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'results of validation run % are written only while it runs (it is %)',
            NEW.run_id, run_status USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""

HOLDOUT_GUARD = """
DECLARE
    run_status text;
BEGIN
    SELECT status INTO run_status FROM validation_runs WHERE id = NEW.run_id FOR SHARE;
    IF run_status IS DISTINCT FROM 'COMPLETED' THEN
        RAISE EXCEPTION 'the holdout of validation run % is evaluated only after it completed',
            NEW.run_id USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
"""


def _guards() -> None:
    for name, body in (
        ("atlas_validation_run_guard", RUN_GUARD),
        ("atlas_validation_result_guard", RESULT_GUARD),
        ("atlas_validation_holdout_guard", HOLDOUT_GUARD),
    ):
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$;"
        )
    op.execute(
        "CREATE TRIGGER validation_runs_guard BEFORE UPDATE OR DELETE ON validation_runs "
        "FOR EACH ROW EXECUTE FUNCTION atlas_validation_run_guard()"
    )
    op.execute(
        "CREATE TRIGGER validation_runs_no_truncate BEFORE TRUNCATE ON validation_runs "
        "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
    )
    for table in RESULT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_while_running BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_validation_result_guard()"
        )
    op.execute(
        "CREATE TRIGGER validation_holdout_evaluations_after_completion BEFORE INSERT ON "
        "validation_holdout_evaluations FOR EACH ROW EXECUTE FUNCTION "
        "atlas_validation_holdout_guard()"
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )


def _drop_guards() -> None:
    for table in APPEND_ONLY:
        for suffix in ("append_only", "no_truncate", "while_running"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix} ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS validation_holdout_evaluations_after_completion ON "
        "validation_holdout_evaluations"
    )
    op.execute("DROP TRIGGER IF EXISTS validation_runs_guard ON validation_runs")
    op.execute("DROP TRIGGER IF EXISTS validation_runs_no_truncate ON validation_runs")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "learning_monitoring_rules",
        sa.Column(
            "subject_kind",
            sa.Enum(
                "STRATEGY",
                "MODEL",
                name="learning_subject_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rule_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "WARN",
                "SUSPEND",
                name="learning_rule_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "action <> 'SUSPEND' OR subject_kind = 'STRATEGY'",
            name=op.f("ck_learning_monitoring_rules_suspend_strategies_only"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_learning_monitoring_rules_reason_required")
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_learning_monitoring_rules_version_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_monitoring_rules")),
        sa.UniqueConstraint(
            "subject_kind",
            "subject_key",
            "version",
            name=op.f("uq_learning_monitoring_rules_subject_kind_subject_key_version"),
        ),
    )
    op.create_table(
        "validation_comparisons",
        sa.Column(
            "kind",
            sa.Enum(
                "STRATEGY",
                "MODEL",
                name="learning_subject_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("subjects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_comparisons")),
    )
    op.create_index(
        op.f("ix_validation_comparisons_created_at"),
        "validation_comparisons",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "validation_experiments",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "WALK_FORWARD",
                "EVALUATION",
                name="validation_experiment_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_ids", sa.ARRAY(sa.String(length=48)), nullable=False),
        sa.Column(
            "search_method",
            sa.Enum(
                "NONE",
                "GRID",
                "RANDOM",
                "BAYESIAN",
                name="validation_search_method",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("max_trials", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "config_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_validation_experiments_config_sha_format"),
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_validation_experiments_always_simulated")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_experiments")),
    )
    op.create_index(
        op.f("ix_validation_experiments_created_at"),
        "validation_experiments",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "learning_drift_measurements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("monitor_run_id", sa.UUID(), nullable=False),
        sa.Column(
            "subject_kind",
            sa.Enum(
                "STRATEGY",
                "MODEL",
                name="learning_subject_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column(
            "metric",
            sa.Enum(
                "EXPECTANCY_SHIFT",
                "WIN_RATE_SHIFT",
                "DRAWDOWN",
                "COST_PER_TRADE",
                "RETURN_DISTRIBUTION",
                "FEATURE_PSI",
                "FEATURE_KS",
                "CALIBRATION_ECE",
                "PREDICTION_SHIFT",
                "REGIME_FAILURE",
                name="learning_monitor_metric",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("feature", sa.String(length=200), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("p_value", sa.Numeric(), nullable=True),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_reference", sa.Integer(), nullable=False),
        sa.Column("n_current", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OK",
                "WARNING",
                "BREACH",
                "INSUFFICIENT_DATA",
                name="learning_measurement_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("rule_id", sa.UUID(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["learning_monitoring_rules.id"],
            name=op.f("fk_learning_drift_measurements_rule_id_learning_monitoring_rules"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_drift_measurements")),
    )
    op.create_index(
        op.f("ix_learning_drift_measurements_monitor_run_id"),
        "learning_drift_measurements",
        ["monitor_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_drift_measurements_subject_kind_subject_key_measured_at"),
        "learning_drift_measurements",
        ["subject_kind", "subject_key", "measured_at"],
        unique=False,
    )
    op.create_table(
        "validation_runs",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="validation_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("base_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("base_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=16), nullable=False),
        sa.Column("code_version", sa.String(length=80), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("data_known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("reproduction_of", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "progress", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prior_trials", sa.Integer(), nullable=True),
        sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "verdict",
            sa.Enum(
                "INSUFFICIENT_EVIDENCE",
                "NOT_ROBUST",
                "CANDIDATE_FOR_REVIEW",
                name="validation_verdict",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "findings", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("holdout_start", sa.Date(), nullable=True),
        sa.Column("holdout_end", sa.Date(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("reproduced_identical", sa.Boolean(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (result_sha256 IS NOT NULL)",
            name=op.f("ck_validation_runs_result_when_completed"),
        ),
        sa.CheckConstraint(
            "base_config_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_validation_runs_base_config_sha_format"),
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name=op.f("ck_validation_runs_failed_has_error"),
        ),
        sa.CheckConstraint("end_date >= start_date", name=op.f("ck_validation_runs_date_order")),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["validation_experiments.id"],
            name=op.f("fk_validation_runs_experiment_id_validation_experiments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reproduction_of"],
            ["validation_runs.id"],
            name=op.f("fk_validation_runs_reproduction_of_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_runs")),
    )
    op.create_index(
        op.f("ix_validation_runs_created_at"), "validation_runs", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_validation_runs_experiment_id"), "validation_runs", ["experiment_id"], unique=False
    )
    op.create_index(op.f("ix_validation_runs_status"), "validation_runs", ["status"], unique=False)
    op.create_table(
        "learning_degradation_alerts",
        sa.Column(
            "subject_kind",
            sa.Enum(
                "STRATEGY",
                "MODEL",
                name="learning_subject_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column(
            "metric",
            sa.Enum(
                "EXPECTANCY_SHIFT",
                "WIN_RATE_SHIFT",
                "DRAWDOWN",
                "COST_PER_TRADE",
                "RETURN_DISTRIBUTION",
                "FEATURE_PSI",
                "FEATURE_KS",
                "CALIBRATION_ECE",
                "PREDICTION_SHIFT",
                "REGIME_FAILURE",
                name="learning_monitor_metric",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("measurement_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "NONE",
                "WARNED",
                "SUSPENDED",
                name="learning_alert_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "action_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["learning_drift_measurements.id"],
            name=op.f("fk_learning_degradation_alerts_measurement_id_learning_drift_measurements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["learning_monitoring_rules.id"],
            name=op.f("fk_learning_degradation_alerts_rule_id_learning_monitoring_rules"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_degradation_alerts")),
    )
    op.create_index(
        op.f("ix_learning_degradation_alerts_subject_kind_subject_key_created_at"),
        "learning_degradation_alerts",
        ["subject_kind", "subject_key", "created_at"],
        unique=False,
    )
    op.create_table(
        "validation_folds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("fold_index", sa.Integer(), nullable=False),
        sa.Column("refit", sa.Boolean(), nullable=False),
        sa.Column("train_start", sa.Date(), nullable=False),
        sa.Column("train_end", sa.Date(), nullable=False),
        sa.Column("validation_start", sa.Date(), nullable=True),
        sa.Column("validation_end", sa.Date(), nullable=True),
        sa.Column("test_start", sa.Date(), nullable=False),
        sa.Column("test_end", sa.Date(), nullable=False),
        sa.Column("embargo_sessions", sa.Integer(), nullable=False),
        sa.Column("selected_trial", sa.Integer(), nullable=False),
        sa.Column("selection", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("in_sample", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("out_of_sample", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("champion_out_of_sample", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("oos_series", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("champion_series", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("oos_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("oos_dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("test_end >= test_start", name=op.f("ck_validation_folds_test_order")),
        sa.CheckConstraint(
            "test_start > train_end", name=op.f("ck_validation_folds_test_after_training")
        ),
        sa.CheckConstraint(
            "validation_start IS NULL OR (validation_start > train_end AND test_start > validation_end)",
            name=op.f("ck_validation_folds_validation_between"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.id"],
            name=op.f("fk_validation_folds_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_folds")),
        sa.UniqueConstraint(
            "run_id", "fold_index", name=op.f("uq_validation_folds_run_id_fold_index")
        ),
    )
    op.create_table(
        "validation_holdout_evaluations",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("strategy_ids", sa.ARRAY(sa.String(length=48)), nullable=False),
        sa.Column("holdout_start", sa.Date(), nullable=False),
        sa.Column("holdout_end", sa.Date(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("access_number", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "access_number >= 1", name=op.f("ck_validation_holdout_evaluations_access_positive")
        ),
        sa.CheckConstraint(
            "holdout_end >= holdout_start",
            name=op.f("ck_validation_holdout_evaluations_date_order"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.id"],
            name=op.f("fk_validation_holdout_evaluations_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_holdout_evaluations")),
    )
    op.create_index(
        op.f("ix_validation_holdout_evaluations_run_id"),
        "validation_holdout_evaluations",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "validation_robustness_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "PURGED_KFOLD",
                "CPCV",
                "PBO",
                "DEFLATED_SHARPE",
                "MULTIPLE_TESTING",
                "PARAMETER_SENSITIVITY",
                "MONTE_CARLO_TRADES",
                "BLOCK_BOOTSTRAP",
                "COST_STRESS",
                "SLIPPAGE_STRESS",
                "REGIME_SEGMENTATION",
                "OOS_DEGRADATION",
                name="validation_robustness_method",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OK",
                "INSUFFICIENT_SAMPLE",
                "NOT_APPLICABLE",
                "NOT_RUN",
                name="validation_result_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assumptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.id"],
            name=op.f("fk_validation_robustness_results_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_robustness_results")),
    )
    op.create_index(
        op.f("ix_validation_robustness_results_run_id_method"),
        "validation_robustness_results",
        ["run_id", "method"],
        unique=False,
    )
    op.create_table(
        "validation_segment_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "sample",
            sa.Enum(
                "IN_SAMPLE",
                "VALIDATION",
                "OUT_OF_SAMPLE",
                "HOLDOUT",
                name="validation_sample",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "segment_kind",
            sa.Enum(
                "OVERALL",
                "STRATEGY",
                "ASSET",
                "ASSET_CLASS",
                "TIMEFRAME",
                "DIRECTION",
                "MARKET_TREND",
                "VOLATILITY",
                "PERIOD",
                name="validation_segment_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("segment_value", sa.String(length=64), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OK",
                "INSUFFICIENT_SAMPLE",
                "NOT_APPLICABLE",
                "NOT_RUN",
                name="validation_result_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.id"],
            name=op.f("fk_validation_segment_results_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_segment_results")),
    )
    op.create_index(
        op.f("ix_validation_segment_results_run_id_segment_kind"),
        "validation_segment_results",
        ["run_id", "segment_kind"],
        unique=False,
    )
    op.create_table(
        "validation_trials",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.String(length=200), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "STORED",
                "GRID",
                "RANDOM",
                "BAYESIAN",
                "NEIGHBOR",
                name="validation_trial_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "OK",
                "FAILED",
                name="validation_trial_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("selectable", sa.Boolean(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=True),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=True),
        sa.Column("development", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "development_returns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parameters_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_validation_trials_parameters_sha_format"),
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error IS NOT NULL",
            name=op.f("ck_validation_trials_failed_has_error"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["validation_runs.id"],
            name=op.f("fk_validation_trials_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_trials")),
        sa.UniqueConstraint(
            "run_id", "trial_number", name=op.f("uq_validation_trials_run_id_trial_number")
        ),
    )
    op.create_table(
        "learning_candidates",
        sa.Column(
            "subject_kind",
            sa.Enum(
                "STRATEGY",
                "MODEL",
                name="learning_subject_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=48), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=True),
        sa.Column("base_strategy_version_id", sa.UUID(), nullable=True),
        sa.Column("model_version_id", sa.UUID(), nullable=True),
        sa.Column("source_run_id", sa.UUID(), nullable=True),
        sa.Column("champion_at_creation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(subject_kind = 'MODEL') = (model_version_id IS NOT NULL AND strategy_id IS NULL)",
            name=op.f("ck_learning_candidates_model_subject"),
        ),
        sa.CheckConstraint(
            "(subject_kind = 'STRATEGY') = (strategy_id IS NOT NULL AND parameters IS NOT NULL AND model_version_id IS NULL)",
            name=op.f("ck_learning_candidates_strategy_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["base_strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_learning_candidates_base_strategy_version_id_strategy_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["ml_model_versions.id"],
            name=op.f("fk_learning_candidates_model_version_id_ml_model_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["validation_runs.id"],
            name=op.f("fk_learning_candidates_source_run_id_validation_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name=op.f("fk_learning_candidates_strategy_id_strategies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_candidates")),
    )
    op.create_index(
        op.f("ix_learning_candidates_subject_kind_subject_key"),
        "learning_candidates",
        ["subject_kind", "subject_key"],
        unique=False,
    )
    op.create_table(
        "learning_candidate_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("previous_event_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "from_status",
            sa.Enum(
                "CANDIDATE",
                "VALIDATING",
                "SHADOW",
                "APPROVED",
                "REJECTED",
                "RETIRED",
                name="learning_candidate_from_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "CANDIDATE",
                "VALIDATING",
                "SHADOW",
                "APPROVED",
                "REJECTED",
                "RETIRED",
                name="learning_candidate_to_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Enum(
                "USER",
                "SYSTEM",
                name="learning_actor_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_learning_candidate_events_reason_required")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["learning_candidates.id"],
            name=op.f("fk_learning_candidate_events_candidate_id_learning_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_event_id"],
            ["learning_candidate_events.id"],
            name=op.f("fk_learning_candidate_events_previous_event_id_learning_candidate_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_candidate_events")),
        sa.UniqueConstraint(
            "previous_event_id", name=op.f("uq_learning_candidate_events_previous_event_id")
        ),
    )
    op.create_index(
        "uq_learning_candidate_events_first",
        "learning_candidate_events",
        ["candidate_id"],
        unique=True,
        postgresql_where=sa.text("previous_event_id IS NULL"),
    )
    op.create_table(
        "learning_approvals",
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "APPROVE",
                "REJECT",
                "RETIRE",
                "ROLLBACK",
                name="learning_decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("approver", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("replaced", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("strategy_version_id", sa.UUID(), nullable=True),
        sa.Column("model_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_learning_approvals_reason_required")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["learning_candidates.id"],
            name=op.f("fk_learning_approvals_candidate_id_learning_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["learning_candidate_events.id"],
            name=op.f("fk_learning_approvals_event_id_learning_candidate_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["ml_model_versions.id"],
            name=op.f("fk_learning_approvals_model_version_id_ml_model_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_learning_approvals_strategy_version_id_strategy_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_approvals")),
    )
    op.create_index(
        op.f("ix_learning_approvals_candidate_id"),
        "learning_approvals",
        ["candidate_id"],
        unique=False,
    )
    # ### end Alembic commands ###
    _guards()


def downgrade() -> None:
    _drop_guards()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_learning_approvals_candidate_id"), table_name="learning_approvals")
    op.drop_table("learning_approvals")
    op.drop_index(
        "uq_learning_candidate_events_first",
        table_name="learning_candidate_events",
        postgresql_where=sa.text("previous_event_id IS NULL"),
    )
    op.drop_table("learning_candidate_events")
    op.drop_index(
        op.f("ix_learning_candidates_subject_kind_subject_key"), table_name="learning_candidates"
    )
    op.drop_table("learning_candidates")
    op.drop_table("validation_trials")
    op.drop_index(
        op.f("ix_validation_segment_results_run_id_segment_kind"),
        table_name="validation_segment_results",
    )
    op.drop_table("validation_segment_results")
    op.drop_index(
        op.f("ix_validation_robustness_results_run_id_method"),
        table_name="validation_robustness_results",
    )
    op.drop_table("validation_robustness_results")
    op.drop_index(
        op.f("ix_validation_holdout_evaluations_run_id"),
        table_name="validation_holdout_evaluations",
    )
    op.drop_table("validation_holdout_evaluations")
    op.drop_table("validation_folds")
    op.drop_index(
        op.f("ix_learning_degradation_alerts_subject_kind_subject_key_created_at"),
        table_name="learning_degradation_alerts",
    )
    op.drop_table("learning_degradation_alerts")
    op.drop_index(op.f("ix_validation_runs_status"), table_name="validation_runs")
    op.drop_index(op.f("ix_validation_runs_experiment_id"), table_name="validation_runs")
    op.drop_index(op.f("ix_validation_runs_created_at"), table_name="validation_runs")
    op.drop_table("validation_runs")
    op.drop_index(
        op.f("ix_learning_drift_measurements_subject_kind_subject_key_measured_at"),
        table_name="learning_drift_measurements",
    )
    op.drop_index(
        op.f("ix_learning_drift_measurements_monitor_run_id"),
        table_name="learning_drift_measurements",
    )
    op.drop_table("learning_drift_measurements")
    op.drop_index(op.f("ix_validation_experiments_created_at"), table_name="validation_experiments")
    op.drop_table("validation_experiments")
    op.drop_index(op.f("ix_validation_comparisons_created_at"), table_name="validation_comparisons")
    op.drop_table("validation_comparisons")
    op.drop_table("learning_monitoring_rules")
    # ### end Alembic commands ###
    for name in (
        "atlas_validation_holdout_guard",
        "atlas_validation_result_guard",
        "atlas_validation_run_guard",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")

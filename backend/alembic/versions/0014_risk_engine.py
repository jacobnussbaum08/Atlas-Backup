"""risk engine (Phase 6B)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-05 22:33:31.779822+00:00

Deterministic risk engine tables (decisions D-138 to D-147, docs/architecture/risk.md).

* ``risk_configurations`` / ``risk_configuration_versions``: immutable, gapless versions,
  written only by an ``operator:`` actor with a reason (``atlas_risk_config_version_guard``).
* ``risk_asset_classifications``: operator sector and correlation-cluster tags.
* ``risk_exposure_snapshots``, ``risk_period_baselines``.
* ``risk_assessments`` / ``risk_assessment_reasons``: ``atlas_risk_assessment_guard`` refuses
  a PASS_FOR_RESERVATION row while a breaker is tripped, against a configuration that is not
  the latest, or with a budget above the configured risk per trade;
  ``atlas_risk_reason_guard`` allows checks only in their assessment's transaction; the
  deferred ``atlas_risk_assessment_consistent`` requires the stored result to follow from the
  stored checks (HALT -> RISK_HALTED, FAIL -> REJECTED, UNKNOWN -> INSUFFICIENT_DATA, else
  PASS_FOR_RESERVATION).
* ``risk_circuit_breaker_events``: ``atlas_risk_breaker_guard`` numbers events per breaker
  without gaps and requires each to start from the previous state; the view
  ``risk_circuit_breaker_states`` is the latest event of each breaker.
* ``risk_events``.

Every table is append-only (``atlas_reject_modification``, migration 0001).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPEND_ONLY_TABLES = (
    "risk_configurations",
    "risk_configuration_versions",
    "risk_asset_classifications",
    "risk_exposure_snapshots",
    "risk_period_baselines",
    "risk_assessments",
    "risk_assessment_reasons",
    "risk_circuit_breaker_events",
    "risk_events",
)

GUARD_FUNCTIONS = {
    "atlas_risk_config_version_guard": """
    DECLARE
        latest record;
        owner uuid;
    BEGIN
        SELECT portfolio_account_id INTO owner FROM risk_configurations
            WHERE id = NEW.configuration_id;
        IF owner IS DISTINCT FROM NEW.portfolio_account_id THEN
            RAISE EXCEPTION 'configuration % does not belong to account %',
                NEW.configuration_id, NEW.portfolio_account_id USING ERRCODE = 'check_violation';
        END IF;
        SELECT id, version INTO latest FROM risk_configuration_versions
            WHERE configuration_id = NEW.configuration_id ORDER BY version DESC LIMIT 1;
        IF latest.id IS NULL THEN
            IF NEW.version <> 1 THEN
                RAISE EXCEPTION 'the first risk configuration version must be 1'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.version <> latest.version + 1
              OR NEW.previous_version_id IS DISTINCT FROM latest.id THEN
            RAISE EXCEPTION 'risk configuration version % must follow version % (%)',
                NEW.version, latest.version, latest.id USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_risk_breaker_guard": """
    DECLARE
        latest record;
    BEGIN
        SELECT sequence, new_status INTO latest FROM risk_circuit_breaker_events
            WHERE portfolio_account_id = NEW.portfolio_account_id
              AND breaker_type = NEW.breaker_type
            ORDER BY sequence DESC LIMIT 1;
        IF NOT FOUND THEN
            IF NEW.sequence <> 1 OR NEW.previous_status <> 'CLEAR' THEN
                RAISE EXCEPTION 'the first % event must be sequence 1 from CLEAR',
                    NEW.breaker_type USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.sequence <> latest.sequence + 1 OR NEW.previous_status <> latest.new_status THEN
            RAISE EXCEPTION '% event % must follow sequence % in state %',
                NEW.breaker_type, NEW.sequence, latest.sequence, latest.new_status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_risk_assessment_guard": """
    DECLARE
        latest_version uuid;
        per_trade numeric;
        tripped text;
    BEGIN
        NEW.created_txid := txid_current();
        IF NEW.result = 'PASS_FOR_RESERVATION' THEN
            SELECT v.id, (v.hard_limits ->> 'risk_per_trade_pct')::numeric
                INTO latest_version, per_trade
                FROM risk_configuration_versions v
                WHERE v.portfolio_account_id = NEW.portfolio_account_id
                ORDER BY v.version DESC LIMIT 1;
            IF latest_version IS NULL
               OR NEW.config_version_id IS DISTINCT FROM latest_version THEN
                RAISE EXCEPTION 'a passing risk assessment must use the latest configuration'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.risk_budget > NEW.equity * per_trade THEN
                RAISE EXCEPTION 'risk budget % exceeds % of equity %',
                    NEW.risk_budget, per_trade, NEW.equity USING ERRCODE = 'check_violation';
            END IF;
            SELECT string_agg(breaker_type, ', ' ORDER BY breaker_type) INTO tripped
                FROM risk_circuit_breaker_states
                WHERE portfolio_account_id = NEW.portfolio_account_id AND status = 'TRIPPED';
            IF tripped IS NOT NULL THEN
                RAISE EXCEPTION 'no risk assessment can pass while breakers are tripped: %',
                    tripped USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_risk_reason_guard": """
    BEGIN
        IF (SELECT created_txid FROM risk_assessments WHERE id = NEW.assessment_id)
                IS DISTINCT FROM txid_current() THEN
            RAISE EXCEPTION 'checks can only be recorded in their assessment''s transaction'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
}

CONSISTENCY_CHECK = """
CREATE FUNCTION atlas_risk_assessment_consistent() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    checked_id uuid;
    stored text;
    expected text;
    total integer;
    halts integer;
    fails integer;
    unknowns integer;
BEGIN
    IF TG_TABLE_NAME = 'risk_assessments' THEN
        checked_id := NEW.id;
    ELSE
        checked_id := NEW.assessment_id;
    END IF;
    SELECT result INTO stored FROM risk_assessments WHERE id = checked_id;
    SELECT count(*),
           count(*) FILTER (WHERE outcome = 'HALT'),
           count(*) FILTER (WHERE outcome = 'FAIL'),
           count(*) FILTER (WHERE outcome = 'UNKNOWN')
        INTO total, halts, fails, unknowns
        FROM risk_assessment_reasons WHERE assessment_id = checked_id;
    IF total = 0 THEN
        RAISE EXCEPTION 'risk assessment % has no recorded checks', checked_id
            USING ERRCODE = 'check_violation';
    END IF;
    expected := CASE
        WHEN halts > 0 THEN 'RISK_HALTED'
        WHEN fails > 0 THEN 'REJECTED'
        WHEN unknowns > 0 THEN 'INSUFFICIENT_DATA'
        ELSE 'PASS_FOR_RESERVATION'
    END;
    IF stored <> expected THEN
        RAISE EXCEPTION 'risk assessment % is % but its checks say %', checked_id, stored, expected
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""

STATES_VIEW = """
CREATE VIEW risk_circuit_breaker_states AS
SELECT DISTINCT ON (portfolio_account_id, breaker_type)
    portfolio_account_id,
    breaker_type,
    new_status AS status,
    sequence,
    event AS last_event,
    occurred_at AS since,
    actor,
    reason,
    id AS event_id
FROM risk_circuit_breaker_events
ORDER BY portfolio_account_id, breaker_type, sequence DESC
"""


def _guards() -> None:
    op.execute(STATES_VIEW)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )
    for name, body in GUARD_FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$;")
    op.execute(
        "CREATE TRIGGER risk_configuration_versions_guard BEFORE INSERT ON "
        "risk_configuration_versions FOR EACH ROW "
        "EXECUTE FUNCTION atlas_risk_config_version_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_circuit_breaker_events_guard BEFORE INSERT ON "
        "risk_circuit_breaker_events FOR EACH ROW EXECUTE FUNCTION atlas_risk_breaker_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_assessments_guard BEFORE INSERT ON risk_assessments "
        "FOR EACH ROW EXECUTE FUNCTION atlas_risk_assessment_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_assessment_reasons_guard BEFORE INSERT ON risk_assessment_reasons "
        "FOR EACH ROW EXECUTE FUNCTION atlas_risk_reason_guard()"
    )
    op.execute(CONSISTENCY_CHECK)
    for table in ("risk_assessments", "risk_assessment_reasons"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_consistent AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION atlas_risk_assessment_consistent()"
        )


def _drop_guards() -> None:
    for table in ("risk_assessments", "risk_assessment_reasons"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_consistent ON {table}")
    op.execute("DROP FUNCTION IF EXISTS atlas_risk_assessment_consistent()")
    op.execute("DROP TRIGGER IF EXISTS risk_assessment_reasons_guard ON risk_assessment_reasons")
    op.execute("DROP TRIGGER IF EXISTS risk_assessments_guard ON risk_assessments")
    op.execute(
        "DROP TRIGGER IF EXISTS risk_circuit_breaker_events_guard ON risk_circuit_breaker_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS risk_configuration_versions_guard ON risk_configuration_versions"
    )
    for name in GUARD_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP VIEW IF EXISTS risk_circuit_breaker_states")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "risk_asset_classifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("sector", sa.String(length=48), nullable=True),
        sa.Column("cluster", sa.String(length=48), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "OPERATOR",
                "TEST_FIXTURE",
                name="risk_classification_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cluster IS NULL OR cluster ~ '^[A-Z0-9_]{2,48}$'",
            name=op.f("ck_risk_asset_classifications_cluster_format"),
        ),
        sa.CheckConstraint(
            "sector IS NULL OR sector ~ '^[A-Z0-9_]{2,48}$'",
            name=op.f("ck_risk_asset_classifications_sector_format"),
        ),
        sa.CheckConstraint(
            "source = 'TEST_FIXTURE' OR created_by ~ '^operator:[A-Za-z0-9._@-]+$'",
            name=op.f("ck_risk_asset_classifications_operator_or_fixture"),
        ),
        sa.CheckConstraint(
            "sector IS NOT NULL OR cluster IS NOT NULL",
            name=op.f("ck_risk_asset_classifications_classifies"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_risk_asset_classifications_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_asset_classifications")),
        sa.UniqueConstraint(
            "asset_id", "valid_from", name=op.f("uq_risk_asset_classifications_asset_id_valid_from")
        ),
    )
    op.create_table(
        "risk_configurations",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_configurations_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_configurations")),
        sa.UniqueConstraint(
            "portfolio_account_id", name=op.f("uq_risk_configurations_portfolio_account_id")
        ),
    )
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "CONFIGURATION_CREATED",
                "CONFIGURATION_CHANGED",
                "ASSESSMENT_RECORDED",
                "BREAKER_TRIPPED",
                "BREAKER_RESET",
                "BREAKER_RESET_REFUSED",
                "STATE_EVALUATED",
                "CLASSIFICATION_RECORDED",
                name="risk_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(
                "INFO",
                "WARNING",
                "HIGH",
                "CRITICAL",
                name="risk_event_severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("subject_kind", sa.String(length=48), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
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
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_events_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_events")),
    )
    op.create_index(
        op.f("ix_risk_events_event_type_occurred_at"),
        "risk_events",
        ["event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_events_portfolio_account_id_occurred_at"),
        "risk_events",
        ["portfolio_account_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "risk_period_baselines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "period_kind",
            sa.Enum(
                "DAILY",
                "WEEKLY",
                name="risk_period_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("period_label", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_equity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "baseline_contributed_capital", sa.Numeric(precision=28, scale=8), nullable=False
        ),
        sa.Column(
            "basis",
            sa.Enum(
                "OBSERVED_AT_BOUNDARY",
                "FUNDED_IN_PERIOD",
                "FIRST_OBSERVATION_IN_PERIOD",
                name="risk_baseline_basis",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("timezone_policy", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_period_baselines_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_period_baselines")),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "period_kind",
            "period_start",
            name=op.f("uq_risk_period_baselines_portfolio_account_id_period_kind_period_start"),
        ),
    )
    op.create_table(
        "risk_configuration_versions",
        sa.Column("configuration_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "change_kind",
            sa.Enum(
                "INITIAL",
                "HARD_LIMITS",
                "PREFERENCES",
                "HARD_LIMITS_AND_PREFERENCES",
                name="risk_config_change_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("hard_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hard_limits_sha256", sa.String(length=64), nullable=False),
        sa.Column("preferences_sha256", sa.String(length=64), nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(version = 1) = (change_kind = 'INITIAL')",
            name=op.f("ck_risk_configuration_versions_initial_is_first"),
        ),
        sa.CheckConstraint(
            "actor ~ '^operator:[A-Za-z0-9._@-]+$'",
            name=op.f("ck_risk_configuration_versions_operator_actor"),
        ),
        sa.CheckConstraint(
            "(version = 1) = (previous_version_id IS NULL)",
            name=op.f("ck_risk_configuration_versions_previous_iff_not_first"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 10", name=op.f("ck_risk_configuration_versions_reason_given")
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_risk_configuration_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"],
            ["risk_configurations.id"],
            name=op.f("fk_risk_configuration_versions_configuration_id_risk_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_configuration_versions_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["risk_configuration_versions.id"],
            name=op.f(
                "fk_risk_configuration_versions_previous_version_id_risk_configuration_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_configuration_versions")),
        sa.UniqueConstraint(
            "configuration_id",
            "version",
            name=op.f("uq_risk_configuration_versions_configuration_id_version"),
        ),
    )
    op.create_index(
        op.f("ix_risk_configuration_versions_portfolio_account_id_version"),
        "risk_configuration_versions",
        ["portfolio_account_id", "version"],
        unique=False,
    )
    op.create_table(
        "risk_exposure_snapshots",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum(
                "ASSESSMENT",
                "MONITOR",
                "BREAKER_RESET",
                name="risk_snapshot_trigger",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "mark_status",
            sa.Enum(
                "COMPLETE",
                "STALE",
                "INCOMPLETE",
                name="risk_mark_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("functional_currency", sa.String(length=3), nullable=False),
        sa.Column("equity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("contributed_capital", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("cash", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("available_capital", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("long_exposure", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("short_exposure", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("gross_exposure", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("net_exposure", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("margin_used", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "pending_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("daily_loss", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("daily_baseline", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("weekly_loss", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("weekly_baseline", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("peak_equity", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("drawdown", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("available_risk_budget", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("positions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "issues", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("config_version_id", sa.UUID(), nullable=True),
        sa.Column("engine_version", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "gross_exposure = long_exposure + short_exposure",
            name=op.f("ck_risk_exposure_snapshots_gross_is_long_plus_short"),
        ),
        sa.CheckConstraint(
            "ledger_sequence >= 0", name=op.f("ck_risk_exposure_snapshots_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "net_exposure = long_exposure - short_exposure",
            name=op.f("ck_risk_exposure_snapshots_net_is_long_minus_short"),
        ),
        sa.CheckConstraint(
            "pending_exposure = 0",
            name=op.f("ck_risk_exposure_snapshots_no_reservations_before_6c"),
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"],
            ["risk_configuration_versions.id"],
            name=op.f("fk_risk_exposure_snapshots_config_version_id_risk_configuration_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["functional_currency"],
            ["currencies.code"],
            name=op.f("fk_risk_exposure_snapshots_functional_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_exposure_snapshots_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_exposure_snapshots")),
    )
    op.create_index(
        op.f("ix_risk_exposure_snapshots_portfolio_account_id_as_of"),
        "risk_exposure_snapshots",
        ["portfolio_account_id", "as_of"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_exposure_snapshots_portfolio_account_id_mark_status_as_of"),
        "risk_exposure_snapshots",
        ["portfolio_account_id", "mark_status", "as_of"],
        unique=False,
    )
    op.create_table(
        "risk_assessments",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("request_key", sa.String(length=160), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "OPERATOR",
                "STRATEGY_PROPOSAL",
                "AI_DECISION",
                name="risk_proposal_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("strategy_proposal_id", sa.UUID(), nullable=True),
        sa.Column("ai_decision_id", sa.UUID(), nullable=True),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "LONG",
                "SHORT",
                name="risk_trade_direction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("entry", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("stop", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("target", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("requested_quantity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column(
            "result",
            sa.Enum(
                "PASS_FOR_RESERVATION",
                "REJECTED",
                "INSUFFICIENT_DATA",
                "RISK_HALTED",
                name="risk_assessment_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("permitted_quantity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("functional_currency", sa.String(length=3), nullable=False),
        sa.Column("equity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("risk_budget", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("potential_loss", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("notional", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("estimated_costs", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("binding_constraint", sa.String(length=48), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inputs_sha256", sa.String(length=64), nullable=False),
        sa.Column("calculations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exposure_after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exposure_snapshot_id", sa.UUID(), nullable=True),
        sa.Column("config_version_id", sa.UUID(), nullable=True),
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "result <> 'PASS_FOR_RESERVATION' OR (permitted_quantity > 0 AND risk_budget > 0 AND potential_loss > 0 AND potential_loss <= risk_budget AND equity > 0 AND config_version_id IS NOT NULL)",
            name=op.f("ck_risk_assessments_pass_is_sized_within_budget"),
        ),
        sa.CheckConstraint(
            "result = 'PASS_FOR_RESERVATION' OR permitted_quantity IS NULL",
            name=op.f("ck_risk_assessments_only_pass_has_quantity"),
        ),
        sa.CheckConstraint("entry > 0", name=op.f("ck_risk_assessments_entry_positive")),
        sa.CheckConstraint(
            "valid_until > assessed_at", name=op.f("ck_risk_assessments_valid_after_assessment")
        ),
        sa.ForeignKeyConstraint(
            ["ai_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_risk_assessments_ai_decision_id_ai_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_risk_assessments_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"],
            ["risk_configuration_versions.id"],
            name=op.f("fk_risk_assessments_config_version_id_risk_configuration_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exposure_snapshot_id"],
            ["risk_exposure_snapshots.id"],
            name=op.f("fk_risk_assessments_exposure_snapshot_id_risk_exposure_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["functional_currency"],
            ["currencies.code"],
            name=op.f("fk_risk_assessments_functional_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_assessments_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_proposal_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_risk_assessments_strategy_proposal_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_assessments")),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "request_key",
            name=op.f("uq_risk_assessments_portfolio_account_id_request_key"),
        ),
    )
    op.create_index(
        op.f("ix_risk_assessments_asset_id_assessed_at"),
        "risk_assessments",
        ["asset_id", "assessed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_assessments_portfolio_account_id_assessed_at"),
        "risk_assessments",
        ["portfolio_account_id", "assessed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_assessments_result_assessed_at"),
        "risk_assessments",
        ["result", "assessed_at"],
        unique=False,
    )
    op.create_table(
        "risk_circuit_breaker_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "breaker_type",
            sa.Enum(
                "DAILY_LOSS",
                "WEEKLY_LOSS",
                "MAX_DRAWDOWN",
                "EXCESSIVE_EXPOSURE",
                "DATA_QUALITY",
                "RECONCILIATION_FAILURE",
                "EXECUTION_SAFETY",
                "KILL_SWITCH",
                name="risk_breaker_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event",
            sa.Enum(
                "TRIPPED",
                "RESET",
                name="risk_breaker_event",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "previous_status",
            sa.Enum(
                "CLEAR",
                "TRIPPED",
                name="risk_breaker_previous_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "new_status",
            sa.Enum(
                "CLEAR",
                "TRIPPED",
                name="risk_breaker_new_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Enum(
                "OPERATOR",
                "SYSTEM",
                name="risk_actor_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "measurements",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "response", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("config_version_id", sa.UUID(), nullable=True),
        sa.Column("exposure_snapshot_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event = 'TRIPPED' AND previous_status = 'CLEAR' AND new_status = 'TRIPPED') OR (event = 'RESET' AND previous_status = 'TRIPPED' AND new_status = 'CLEAR')",
            name=op.f("ck_risk_circuit_breaker_events_valid_transition"),
        ),
        sa.CheckConstraint(
            "actor_kind <> 'OPERATOR' OR actor ~ '^operator:[A-Za-z0-9._@-]+$'",
            name=op.f("ck_risk_circuit_breaker_events_operator_actor_format"),
        ),
        sa.CheckConstraint(
            "actor_kind = 'OPERATOR' OR actor !~ '^operator:'",
            name=op.f("ck_risk_circuit_breaker_events_operator_prefix_matches"),
        ),
        sa.CheckConstraint(
            "breaker_type <> 'KILL_SWITCH' OR actor_kind = 'OPERATOR'",
            name=op.f("ck_risk_circuit_breaker_events_kill_switch_operator"),
        ),
        sa.CheckConstraint(
            "event <> 'RESET' OR actor_kind = 'OPERATOR' OR breaker_type = 'DATA_QUALITY'",
            name=op.f("ck_risk_circuit_breaker_events_manual_reset"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_risk_circuit_breaker_events_reason_given")
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_risk_circuit_breaker_events_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"],
            ["risk_configuration_versions.id"],
            name=op.f(
                "fk_risk_circuit_breaker_events_config_version_id_risk_configuration_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exposure_snapshot_id"],
            ["risk_exposure_snapshots.id"],
            name=op.f(
                "fk_risk_circuit_breaker_events_exposure_snapshot_id_risk_exposure_snapshots"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_circuit_breaker_events_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_circuit_breaker_events")),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "breaker_type",
            "sequence",
            name=op.f("uq_risk_circuit_breaker_events_portfolio_account_id_breaker_type_sequence"),
        ),
    )
    op.create_index(
        op.f("ix_risk_circuit_breaker_events_portfolio_account_id_occurred_at"),
        "risk_circuit_breaker_events",
        ["portfolio_account_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "risk_assessment_reasons",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("assessment_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("check_code", sa.String(length=48), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "PASS",
                "FAIL",
                "UNKNOWN",
                "HALT",
                "RESTRICT",
                "INFO",
                name="risk_check_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=48), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("measured", sa.Numeric(), nullable=True),
        sa.Column("limit_value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_risk_assessment_reasons_assessment_id_risk_assessments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_assessment_reasons")),
        sa.UniqueConstraint(
            "assessment_id",
            "ordinal",
            name=op.f("uq_risk_assessment_reasons_assessment_id_ordinal"),
        ),
    )
    op.create_index(
        op.f("ix_risk_assessment_reasons_reason_code"),
        "risk_assessment_reasons",
        ["reason_code"],
        unique=False,
    )
    # ### end Alembic commands ###
    _guards()


def downgrade() -> None:
    _drop_guards()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        op.f("ix_risk_assessment_reasons_reason_code"), table_name="risk_assessment_reasons"
    )
    op.drop_table("risk_assessment_reasons")
    op.drop_index(
        op.f("ix_risk_circuit_breaker_events_portfolio_account_id_occurred_at"),
        table_name="risk_circuit_breaker_events",
    )
    op.drop_table("risk_circuit_breaker_events")
    op.drop_index(op.f("ix_risk_assessments_result_assessed_at"), table_name="risk_assessments")
    op.drop_index(
        op.f("ix_risk_assessments_portfolio_account_id_assessed_at"), table_name="risk_assessments"
    )
    op.drop_index(op.f("ix_risk_assessments_asset_id_assessed_at"), table_name="risk_assessments")
    op.drop_table("risk_assessments")
    op.drop_index(
        op.f("ix_risk_exposure_snapshots_portfolio_account_id_mark_status_as_of"),
        table_name="risk_exposure_snapshots",
    )
    op.drop_index(
        op.f("ix_risk_exposure_snapshots_portfolio_account_id_as_of"),
        table_name="risk_exposure_snapshots",
    )
    op.drop_table("risk_exposure_snapshots")
    op.drop_index(
        op.f("ix_risk_configuration_versions_portfolio_account_id_version"),
        table_name="risk_configuration_versions",
    )
    op.drop_table("risk_configuration_versions")
    op.drop_table("risk_period_baselines")
    op.drop_index(op.f("ix_risk_events_portfolio_account_id_occurred_at"), table_name="risk_events")
    op.drop_index(op.f("ix_risk_events_event_type_occurred_at"), table_name="risk_events")
    op.drop_table("risk_events")
    op.drop_table("risk_configurations")
    op.drop_table("risk_asset_classifications")
    # ### end Alembic commands ###

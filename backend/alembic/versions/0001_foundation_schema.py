"""Foundation schema: assets, asset identifiers, portfolio accounts, audit logs.

Revision ID: 0001
Revises:
Create Date: 2026-10-30

Notes
* All timestamps are TIMESTAMPTZ; sessions are pinned to UTC by the engine.
* portfolio_accounts has no balance columns: the ledger (Phase 6) is authoritative.
* portfolio_accounts.account_type is constrained to 'PAPER' (paper-only isolation).
* audit_logs is append-only, enforced by triggers that reject UPDATE/DELETE/TRUNCATE.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _str_enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    # ------------------------------------------------------------------ assets
    op.create_table(
        "assets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column(
            "asset_class",
            _str_enum("asset_class", "EQUITY", "ETF", "FOREX", "CRYPTO"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("exchange_code", sa.String(length=32), nullable=True),
        sa.Column("quote_currency", sa.String(length=16), nullable=False),
        sa.Column("base_currency", sa.String(length=16), nullable=True),
        sa.Column(
            "status",
            _str_enum("asset_status", "ACTIVE", "INACTIVE", "DELISTED"),
            server_default="ACTIVE",
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint(
            "symbol",
            "asset_class",
            "exchange_code",
            name=op.f("uq_assets_symbol_asset_class_exchange_code"),
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "symbol = upper(symbol) AND length(symbol) > 0", name=op.f("ck_assets_symbol_upper")
        ),
        sa.CheckConstraint(
            "quote_currency = upper(quote_currency)", name=op.f("ck_assets_quote_currency_upper")
        ),
        sa.CheckConstraint(
            "base_currency IS NULL OR base_currency = upper(base_currency)",
            name=op.f("ck_assets_base_currency_upper"),
        ),
        sa.CheckConstraint(
            "asset_class NOT IN ('FOREX', 'CRYPTO') OR base_currency IS NOT NULL",
            name=op.f("ck_assets_pair_has_base_currency"),
        ),
    )
    op.create_index(op.f("ix_assets_asset_class_status"), "assets", ["asset_class", "status"])

    # ------------------------------------------------------- asset_identifiers
    op.create_table(
        "asset_identifiers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_symbol", sa.String(length=128), nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_identifiers")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_identifiers_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_symbol",
            "valid_from",
            name=op.f("uq_asset_identifiers_provider_provider_symbol_valid_from"),
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name=op.f("ck_asset_identifiers_valid_period_ordered"),
        ),
    )
    op.create_index(op.f("ix_asset_identifiers_asset_id"), "asset_identifiers", ["asset_id"])
    op.create_index(
        "uq_asset_identifiers_current_provider_symbol",
        "asset_identifiers",
        ["provider", "provider_symbol"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    # ------------------------------------------------------ portfolio_accounts
    op.create_table(
        "portfolio_accounts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "account_type",
            _str_enum("portfolio_account_type", "PAPER"),
            server_default="PAPER",
            nullable=False,
        ),
        sa.Column("base_currency", sa.String(length=3), server_default="USD", nullable=False),
        sa.Column(
            "status",
            _str_enum("portfolio_account_status", "ACTIVE", "SUSPENDED", "CLOSED"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("description", sa.String(length=1024), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_accounts")),
        sa.UniqueConstraint("name", name=op.f("uq_portfolio_accounts_name")),
        sa.CheckConstraint(
            "base_currency = upper(base_currency) AND length(base_currency) = 3",
            name=op.f("ck_portfolio_accounts_base_currency_iso"),
        ),
    )

    # -------------------------------------------------------------- audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            _str_enum("audit_actor_type", "USER", "SYSTEM", "SUBSYSTEM"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("previous_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_occurred_at"), "audit_logs", ["occurred_at"])
    op.create_index(
        op.f("ix_audit_logs_entity_type_entity_id"), "audit_logs", ["entity_type", "entity_id"]
    )
    op.create_index(op.f("ix_audit_logs_action"), "audit_logs", ["action"])

    # Append-only enforcement. Reusable by later immutable tables (journal entries, fills).
    op.execute(
        """
        CREATE FUNCTION atlas_reject_modification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS atlas_reject_modification()")
    op.drop_index(op.f("ix_audit_logs_action"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_entity_type_entity_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_occurred_at"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("portfolio_accounts")
    op.drop_index("uq_asset_identifiers_current_provider_symbol", table_name="asset_identifiers")
    op.drop_index(op.f("ix_asset_identifiers_asset_id"), table_name="asset_identifiers")
    op.drop_table("asset_identifiers")
    op.drop_index(op.f("ix_assets_asset_class_status"), table_name="assets")
    op.drop_table("assets")

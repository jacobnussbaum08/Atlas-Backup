"""Market-data provider registry, feeds, and asset-provider mapping integrity.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29

* market_data_providers / market_data_feeds: provider and feed identity with
  referential integrity. Feed coverage distinguishes single-venue (IEX) from
  consolidated (SIP) data.
* Seeds Alpaca and the three feeds Atlas supports for US equities. Values are
  literals (a migration is an immutable snapshot); an integration test checks
  they match the adapter's declared capabilities.
* asset_identifiers (asset-provider mappings): provider FK, provider exchange,
  mapping status, and exclusion constraints that forbid overlapping validity
  periods, so a symbol resolves to exactly one asset at any point in time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE_FORMAT = "code ~ '^[a-z][a-z0-9_]{1,31}$'"


def upgrade() -> None:
    # ------------------------------------------------------ provider registry
    op.create_table(
        "market_data_providers",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_market_data_providers")),
        sa.CheckConstraint(_CODE_FORMAT, name=op.f("ck_market_data_providers_code_format")),
    )
    op.create_table(
        "market_data_feeds",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "coverage",
            sa.Enum(
                "SINGLE_VENUE",
                "CONSOLIDATED",
                name="feed_coverage",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("delay_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider", "code", name=op.f("pk_market_data_feeds")),
        sa.ForeignKeyConstraint(
            ["provider"],
            ["market_data_providers.code"],
            name=op.f("fk_market_data_feeds_provider_market_data_providers"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_CODE_FORMAT, name=op.f("ck_market_data_feeds_code_format")),
        sa.CheckConstraint(
            "delay_seconds >= 0", name=op.f("ck_market_data_feeds_delay_non_negative")
        ),
    )

    op.execute(
        "INSERT INTO market_data_providers (code, display_name) VALUES ('alpaca', 'Alpaca Markets')"
    )
    op.execute(
        """
        INSERT INTO market_data_feeds (provider, code, display_name, coverage, delay_seconds) VALUES
            ('alpaca', 'iex', 'IEX', 'SINGLE_VENUE', 0),
            ('alpaca', 'sip', 'SIP (CTA + UTP)', 'CONSOLIDATED', 0),
            ('alpaca', 'delayed_sip', 'SIP, 15-minute delay', 'CONSOLIDATED', 900)
        """
    )

    # ------------------------------------------------ asset-provider mappings
    op.add_column(
        "asset_identifiers", sa.Column("provider_exchange", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "asset_identifiers",
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "SUSPENDED",
                "RETIRED",
                name="mapping_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="ACTIVE",
            nullable=False,
        ),
    )
    op.execute("UPDATE asset_identifiers SET status = 'RETIRED' WHERE valid_to IS NOT NULL")
    op.create_check_constraint(
        op.f("ck_asset_identifiers_retired_iff_ended"),
        "asset_identifiers",
        "(status = 'RETIRED') = (valid_to IS NOT NULL)",
    )

    # Fail with a clear message instead of an opaque FK error if pre-existing rows
    # reference providers that are not registered. Providers are never invented here.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM asset_identifiers ai
                WHERE NOT EXISTS (SELECT 1 FROM market_data_providers p WHERE p.code = ai.provider)
            ) THEN
                RAISE EXCEPTION 'asset_identifiers contains unregistered providers; register them first';
            END IF;
        END $$;
        """
    )
    op.create_foreign_key(
        op.f("fk_asset_identifiers_provider_market_data_providers"),
        "asset_identifiers",
        "market_data_providers",
        ["provider"],
        ["code"],
        ondelete="RESTRICT",
    )

    # btree_gist lets equality on text columns share a GiST exclusion with a range.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE asset_identifiers
            ADD CONSTRAINT ex_asset_identifiers_symbol_period EXCLUDE USING gist (
                provider WITH =,
                provider_symbol WITH =,
                tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)') WITH &&
            )
        """
    )
    op.execute(
        """
        ALTER TABLE asset_identifiers
            ADD CONSTRAINT ex_asset_identifiers_asset_period EXCLUDE USING gist (
                asset_id WITH =,
                provider WITH =,
                tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)') WITH &&
            )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE asset_identifiers DROP CONSTRAINT IF EXISTS ex_asset_identifiers_asset_period"
    )
    op.execute(
        "ALTER TABLE asset_identifiers DROP CONSTRAINT IF EXISTS ex_asset_identifiers_symbol_period"
    )
    # The btree_gist extension is left installed: other objects may depend on it.
    op.drop_constraint(
        op.f("fk_asset_identifiers_provider_market_data_providers"),
        "asset_identifiers",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_asset_identifiers_retired_iff_ended"), "asset_identifiers", type_="check"
    )
    op.drop_column("asset_identifiers", "status")
    op.drop_column("asset_identifiers", "provider_exchange")
    op.drop_table("market_data_feeds")
    op.drop_table("market_data_providers")

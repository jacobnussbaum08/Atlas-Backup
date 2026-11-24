"""date-bounded research exclusions (D-242)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-17

``research_exclusions`` gains a second scope. ``ASSET`` (X1, D-240) removes an asset from
every research universe. ``WINDOW`` (X2, D-242) marks contaminated observations of one
asset: ``contaminated_from``..``contaminated_to`` (sessions, inclusive) and, for an
identity splice inside a membership span, ``blocked_through`` (the end of the span formed
on the predecessor's data). The replay derives the decisions to block from these and its
own read window. Existing rows are ASSET rows and keep every value (the new columns are
NULL for them). One asset may carry several events in one set, so the entry key becomes
(code, asset, event session). The table stays append-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

TABLE = "research_exclusions"
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WINDOW_RULE = (
    "(scope = 'ASSET' AND contaminated_from IS NULL AND contaminated_to IS NULL "
    "AND blocked_through IS NULL) OR "
    "(scope = 'WINDOW' AND contaminated_from IS NOT NULL AND contaminated_to IS NOT NULL "
    "AND contaminated_from <= contaminated_to "
    "AND event_session BETWEEN contaminated_from AND contaminated_to "
    "AND (blocked_through IS NULL OR blocked_through >= contaminated_to))"
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("contaminated_from", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("contaminated_to", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("blocked_through", sa.Date(), nullable=True))
    op.drop_constraint(op.f("ck_research_exclusions_scope"), TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_research_exclusions_scope"), TABLE, "scope IN ('ASSET', 'WINDOW')"
    )
    op.create_check_constraint(op.f("ck_research_exclusions_window"), TABLE, WINDOW_RULE)
    op.drop_constraint("uq_research_exclusions_entry", TABLE, type_="unique")
    op.create_unique_constraint(
        "uq_research_exclusions_entry", TABLE, ["code", "asset_id", "event_session"]
    )


def downgrade() -> None:
    windows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM research_exclusions WHERE scope <> 'ASSET'")
    )
    if windows:
        raise RuntimeError(
            f"{TABLE} holds {windows} WINDOW exclusion(s): downgrading would discard them "
            "(the table is append-only research evidence)"
        )
    op.drop_constraint("uq_research_exclusions_entry", TABLE, type_="unique")
    op.create_unique_constraint("uq_research_exclusions_entry", TABLE, ["code", "asset_id"])
    op.drop_constraint(op.f("ck_research_exclusions_window"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_research_exclusions_scope"), TABLE, type_="check")
    op.create_check_constraint(op.f("ck_research_exclusions_scope"), TABLE, "scope IN ('ASSET')")
    op.drop_column(TABLE, "blocked_through")
    op.drop_column(TABLE, "contaminated_to")
    op.drop_column(TABLE, "contaminated_from")

"""Add co2_grams column to resource_summaries (omitted from migration 017).

Revision ID: 018
Revises: 017
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: str = "017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "resource_summaries" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("resource_summaries")}

    if "co2_grams" not in existing_cols:
        op.add_column("resource_summaries", sa.Column("co2_grams", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "resource_summaries" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("resource_summaries")}

    if "co2_grams" in existing_cols:
        op.drop_column("resource_summaries", "co2_grams")

"""Add energy_wh, chip_name, tdp_watts columns to resource_summaries.

Revision ID: 017
Revises: 016
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: str = "016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "resource_summaries" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("resource_summaries")}

    if "energy_wh" not in existing_cols:
        op.add_column("resource_summaries", sa.Column("energy_wh", sa.Numeric(10, 4), nullable=True))
    if "chip_name" not in existing_cols:
        op.add_column("resource_summaries", sa.Column("chip_name", sa.String(128), nullable=True))
    if "tdp_watts" not in existing_cols:
        op.add_column("resource_summaries", sa.Column("tdp_watts", sa.Numeric(8, 2), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "resource_summaries" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("resource_summaries")}

    if "tdp_watts" in existing_cols:
        op.drop_column("resource_summaries", "tdp_watts")
    if "chip_name" in existing_cols:
        op.drop_column("resource_summaries", "chip_name")
    if "energy_wh" in existing_cols:
        op.drop_column("resource_summaries", "energy_wh")

"""Add project_settings table for DB-backed configuration persistence.

Revision ID: 030
Revises: 029
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_settings_key", "project_settings", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_project_settings_key", table_name="project_settings")
    op.drop_table("project_settings")

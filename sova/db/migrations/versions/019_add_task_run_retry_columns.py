"""Add retry_of_id and retry_count columns to task_runs.

Revision ID: 019
Revises: 018
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str = "018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "task_runs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("task_runs")}

    if "retry_of_id" not in existing_cols:
        op.add_column("task_runs", sa.Column("retry_of_id", sa.Integer(), nullable=True))

    if "retry_count" not in existing_cols:
        op.add_column("task_runs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "task_runs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("task_runs")}

    if "retry_count" in existing_cols:
        op.drop_column("task_runs", "retry_count")

    if "retry_of_id" in existing_cols:
        op.drop_column("task_runs", "retry_of_id")

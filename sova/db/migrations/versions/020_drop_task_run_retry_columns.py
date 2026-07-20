"""Drop retry_of_id and retry_count columns from task_runs.

The auto-retry system was removed. These columns were added in revision 019
but are no longer referenced by the ORM. This migration drops them so
existing databases are consistent with the current schema.

Revision ID: 020
Revises: 019
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: str = "019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "task_runs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("task_runs")}

    with op.batch_alter_table("task_runs") as batch_op:
        if "retry_of_id" in existing_cols:
            batch_op.drop_column("retry_of_id")
        if "retry_count" in existing_cols:
            batch_op.drop_column("retry_count")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "task_runs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("task_runs")}

    with op.batch_alter_table("task_runs") as batch_op:
        if "retry_count" not in existing_cols:
            batch_op.add_column(sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
        if "retry_of_id" not in existing_cols:
            batch_op.add_column(sa.Column("retry_of_id", sa.Integer(), nullable=True))

"""Make issue_number nullable on task_runs, add run_label column.

Enables issue-less agent runs (e.g., planning roles that operate at
project scope rather than on a single issue).

Revision ID: 008
Revises: 007
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str = "007"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("task_runs"):
        return

    # Make issue_number nullable (SQLite requires batch mode for ALTER COLUMN)
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.alter_column("issue_number", existing_type=sa.String(50), nullable=True)

    # Add run_label column for human-readable identification of issue-less runs
    if not _column_exists("task_runs", "run_label"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.add_column(sa.Column("run_label", sa.String(200), server_default=""))
            batch_op.create_index("ix_task_runs_run_label", ["run_label"])


def downgrade() -> None:
    if not _table_exists("task_runs"):
        return

    if _column_exists("task_runs", "run_label"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.drop_index("ix_task_runs_run_label")
            batch_op.drop_column("run_label")

    # Backfill NULL issue_number values before restoring NOT NULL constraint
    op.execute(sa.text("UPDATE task_runs SET issue_number = '0' WHERE issue_number IS NULL"))

    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.alter_column("issue_number", existing_type=sa.String(50), nullable=False)

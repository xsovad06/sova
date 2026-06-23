"""Add retry_count column to step_executions.

Tracks how many retry attempts preceded each step execution record,
enabling the dashboard to display retry metrics per step.

Limitation: pre-existing StepExecution records are backfilled with
retry_count=0 (the server_default). Historical retry attempts created
before this migration will show retry_count=0, which may be inaccurate
for records that were actually retry attempts. retry_count is only
accurate for records created after this migration.

Revision ID: 009
Revises: 008
Create Date: 2026-06-22T05:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str = "008"
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
    if not _table_exists("step_executions"):
        return

    if not _column_exists("step_executions", "retry_count"):
        with op.batch_alter_table("step_executions") as batch_op:
            batch_op.add_column(sa.Column("retry_count", sa.Integer, server_default="0", nullable=False))


def downgrade() -> None:
    if not _table_exists("step_executions"):
        return

    if _column_exists("step_executions", "retry_count"):
        with op.batch_alter_table("step_executions") as batch_op:
            batch_op.drop_column("retry_count")

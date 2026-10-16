"""Add indexes on task_runs.started_at and cost_records.recorded_at.

Both columns are sorted on in 32+ call sites (`order_by(started_at.desc())`,
dashboard cost queries) with no supporting index.

Revision ID: 039
Revises: 038
Create Date: 2026-10-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: str = "038"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    indexes = [idx["name"] for idx in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    if _table_exists("task_runs") and not _index_exists("task_runs", "ix_task_runs_started"):
        op.create_index("ix_task_runs_started", "task_runs", ["started_at"])

    if _table_exists("cost_records") and not _index_exists("cost_records", "ix_cost_records_recorded"):
        op.create_index("ix_cost_records_recorded", "cost_records", ["recorded_at"])


def downgrade() -> None:
    if _table_exists("task_runs") and _index_exists("task_runs", "ix_task_runs_started"):
        op.drop_index("ix_task_runs_started", table_name="task_runs")

    if _table_exists("cost_records") and _index_exists("cost_records", "ix_cost_records_recorded"):
        op.drop_index("ix_cost_records_recorded", table_name="cost_records")

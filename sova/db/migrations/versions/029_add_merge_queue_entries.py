"""Add merge_queue_entries table for tracking PRs in GitHub merge queues.

The MergeQueueMonitor background service polls these entries to detect
when a queued PR merges and run post-merge cleanup (branch deletion,
issue state transition).

Revision ID: 029
Revises: 028
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: str = "028"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("merge_queue_entries"):
        return

    op.create_table(
        "merge_queue_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("issue_number", sa.String(50), nullable=True),
        sa.Column("project_dir", sa.String(500), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_run_id", sa.Integer, sa.ForeignKey("task_runs.id"), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_user", sa.String(100), server_default=""),
        sa.Column("branch_name", sa.String(300), server_default=""),
    )
    op.create_index("ix_merge_queue_status", "merge_queue_entries", ["status"])
    op.create_index("ix_merge_queue_pr_repo", "merge_queue_entries", ["pr_number", "repo"])


def downgrade() -> None:
    if _table_exists("merge_queue_entries"):
        op.drop_index("ix_merge_queue_pr_repo", table_name="merge_queue_entries")
        op.drop_index("ix_merge_queue_status", table_name="merge_queue_entries")
        op.drop_table("merge_queue_entries")

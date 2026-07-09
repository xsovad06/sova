"""Add pr_creation_queue table for throttled PR creation.

Revision ID: 016
Revises: 015
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str = "015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "pr_creation_queue" not in existing:
        op.create_table(
            "pr_creation_queue",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("task_run_id", sa.Integer, sa.ForeignKey("task_runs.id"), nullable=False),
            sa.Column("issue_number", sa.String(50), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column("base_branch", sa.String(200), nullable=False),
            sa.Column("head_branch", sa.String(200), nullable=False),
            sa.Column("repo", sa.String(200), nullable=False, server_default=""),
            sa.Column("github_user", sa.String(100), nullable=False, server_default=""),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("pr_number", sa.Integer, nullable=True),
            sa.Column("pr_url", sa.String(500), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("project_slug", sa.String(100), server_default=""),
            sa.Column("enqueued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_pr_queue_status", "pr_creation_queue", ["status"])
        op.create_index("ix_pr_queue_task_run", "pr_creation_queue", ["task_run_id"])
        op.create_index("ix_pr_queue_enqueued", "pr_creation_queue", ["enqueued_at"])
        op.create_index(
            "ix_pr_queue_project_status_enqueued",
            "pr_creation_queue",
            ["project_slug", "status", "enqueued_at"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "pr_creation_queue" in existing:
        op.drop_index("ix_pr_queue_project_status_enqueued", table_name="pr_creation_queue")
        op.drop_index("ix_pr_queue_enqueued", table_name="pr_creation_queue")
        op.drop_index("ix_pr_queue_task_run", table_name="pr_creation_queue")
        op.drop_index("ix_pr_queue_status", table_name="pr_creation_queue")
        op.drop_table("pr_creation_queue")

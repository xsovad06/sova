"""Add issue_lifecycles and lifecycle_phases tables, lifecycle_id FK on task_runs.

Revision ID: 006
Revises: 005
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str = "005"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _table_exists("issue_lifecycles"):
        op.create_table(
            "issue_lifecycles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("issue_number", sa.String(50), nullable=False),
            sa.Column("project_slug", sa.String(100), server_default=""),
            sa.Column("current_phase", sa.String(30), nullable=False, server_default="development"),
            sa.Column("phase_status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("pr_number", sa.Integer(), nullable=True),
            sa.Column("branch_name", sa.String(200), server_default=""),
            sa.Column("total_cost_usd", sa.Numeric(10, 6), server_default="0"),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_issue_lifecycles_issue", "issue_lifecycles", ["issue_number"])
        op.create_index("ix_issue_lifecycles_project", "issue_lifecycles", ["project_slug"])

    if not _table_exists("lifecycle_phases"):
        op.create_table(
            "lifecycle_phases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("lifecycle_id", sa.Integer(), sa.ForeignKey("issue_lifecycles.id"), nullable=False),
            sa.Column("phase", sa.String(30), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("task_run_id", sa.Integer(), sa.ForeignKey("task_runs.id"), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0"),
            sa.Column("attempt", sa.Integer(), server_default="1"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_lifecycle_phases_lifecycle", "lifecycle_phases", ["lifecycle_id"])

    if _table_exists("task_runs") and not _column_exists("task_runs", "lifecycle_id"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.add_column(sa.Column("lifecycle_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _table_exists("task_runs") and _column_exists("task_runs", "lifecycle_id"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.drop_column("lifecycle_id")

    if _table_exists("lifecycle_phases"):
        op.drop_table("lifecycle_phases")

    if _table_exists("issue_lifecycles"):
        op.drop_table("issue_lifecycles")

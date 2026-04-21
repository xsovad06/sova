"""Initial schema -- all SOVA tables.

Revision ID: 001
Revises: None
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("issue_number", sa.String(50), nullable=False),
        sa.Column("role", sa.String(50), nullable=False, server_default="developer"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("current_step", sa.String(50)),
        sa.Column("branch_name", sa.String(200), server_default=""),
        sa.Column("worktree_path", sa.String(500), server_default=""),
        sa.Column("pr_number", sa.Integer),
        sa.Column("pid", sa.Integer),
        sa.Column("total_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text),
        sa.Column("project_slug", sa.String(100), server_default=""),
        sa.Column("assessment_json", sa.JSON),
        sa.Column("handoff_json", sa.JSON),
        sa.Column("resumed_from_id", sa.Integer),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_task_runs_issue", "task_runs", ["issue_number"])
    op.create_index("ix_task_runs_status", "task_runs", ["status"])
    op.create_index("ix_task_runs_project", "task_runs", ["project_slug"])

    op.create_table(
        "step_executions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_run_id", sa.Integer, nullable=False),
        sa.Column("step_name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, server_default="0"),
        sa.Column("output_summary", sa.Text),
        sa.Column("gate_check_result", sa.Text),
        sa.Column("error_message", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "failure_records",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_run_id", sa.Integer, nullable=False),
        sa.Column("step_name", sa.String(50), nullable=False),
        sa.Column("failure_type", sa.String(50), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("context", sa.JSON),
        sa.Column("resolved", sa.Boolean, server_default="0"),
        sa.Column("resolved_by", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "cost_records",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_run_id", sa.Integer),
        sa.Column("phase", sa.String(50), nullable=False),
        sa.Column("issue", sa.String(50)),
        sa.Column("model", sa.String(50)),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tags", sa.String(500), server_default=""),
        sa.Column("repo", sa.String(200), server_default=""),
        sa.Column("issue_number", sa.String(50)),
        sa.Column("tier", sa.String(20), server_default="project"),
        sa.Column("superseded_by", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memories_category", "memories", ["category"])
    op.create_index("ix_memories_tags", "memories", ["tags"])

    op.create_table(
        "task_assessments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("issue_number", sa.String(50), nullable=False),
        sa.Column("suitability", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("reasoning", sa.Text),
        sa.Column("missing_context", sa.JSON),
        sa.Column("estimated_complexity", sa.String(30)),
        sa.Column("suggested_role", sa.String(50)),
        sa.Column("sub_tasks", sa.JSON),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_assessments_issue", "task_assessments", ["issue_number"])
    op.create_index("ix_task_assessments_suitability", "task_assessments", ["suitability"])


def downgrade() -> None:
    op.drop_table("task_assessments")
    op.drop_table("memories")
    op.drop_table("cost_records")
    op.drop_table("failure_records")
    op.drop_table("step_executions")
    op.drop_table("task_runs")

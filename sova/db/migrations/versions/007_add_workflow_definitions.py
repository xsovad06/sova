"""Add workflow_definitions and command_contracts tables, workflow_definition_id FK on task_runs.

Revision ID: 007
Revises: 006
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str = "006"
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
    if not _table_exists("workflow_definitions"):
        op.create_table(
            "workflow_definitions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("description", sa.Text(), server_default=""),
            sa.Column("graph_json", sa.JSON(), nullable=False),
            sa.Column("input_states", sa.JSON(), nullable=True),
            sa.Column("output_state", sa.String(50), server_default=""),
            sa.Column("version", sa.Integer(), server_default="1"),
            sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_workflow_definitions_name", "workflow_definitions", ["name"])

    if not _table_exists("command_contracts"):
        op.create_table(
            "command_contracts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("command_name", sa.String(100), nullable=False, unique=True),
            sa.Column("inputs", sa.JSON(), nullable=True),
            sa.Column("outputs", sa.JSON(), nullable=True),
            sa.Column("estimated_cost_usd", sa.Numeric(10, 4), server_default="0"),
            sa.Column("estimated_duration_s", sa.Integer(), server_default="60"),
            sa.Column("idempotent", sa.Boolean(), server_default=sa.text("0")),
            sa.Column("max_retries", sa.Integer(), server_default="0"),
        )
        op.create_index("ix_command_contracts_name", "command_contracts", ["command_name"])

    if _table_exists("task_runs") and not _column_exists("task_runs", "workflow_definition_id"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.add_column(sa.Column("workflow_definition_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_task_runs_workflow_definition",
                "workflow_definitions",
                ["workflow_definition_id"],
                ["id"],
            )
            batch_op.create_index("ix_task_runs_workflow", ["workflow_definition_id"])


def downgrade() -> None:
    if _table_exists("task_runs") and _column_exists("task_runs", "workflow_definition_id"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.drop_index("ix_task_runs_workflow")
            batch_op.drop_column("workflow_definition_id")

    if _table_exists("command_contracts"):
        op.drop_table("command_contracts")

    if _table_exists("workflow_definitions"):
        op.drop_table("workflow_definitions")

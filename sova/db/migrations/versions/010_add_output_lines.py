"""Add output_lines table for DB-backed agent output persistence.

Replaces filesystem-based .claude/agent-output/<run_id>.log files with
database storage, enabling configurable retention and queryability.

Revision ID: 010
Revises: 009
Create Date: 2026-06-26T10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str = "009"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("output_lines"):
        return

    op.create_table(
        "output_lines",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_run_id", sa.Integer, sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("line_number", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_output_lines_run_lineno", "output_lines", ["task_run_id", "line_number"])


def downgrade() -> None:
    if not _table_exists("output_lines"):
        return

    op.drop_index("ix_output_lines_run_lineno", table_name="output_lines")
    op.drop_table("output_lines")

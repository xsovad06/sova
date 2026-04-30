"""Add project_slug to task_assessments.

Revision ID: 003
Revises: 002
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str = "002"
branch_labels: str | None = None
depends_on: str | None = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result)


def upgrade() -> None:
    if _has_column("task_assessments", "project_slug"):
        return
    op.add_column(
        "task_assessments",
        sa.Column("project_slug", sa.String(100), server_default=""),
    )


def downgrade() -> None:
    op.drop_column("task_assessments", "project_slug")

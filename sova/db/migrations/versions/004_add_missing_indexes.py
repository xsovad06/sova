"""Add indexes on project_slug and superseded_by.

Revision ID: 004
Revises: 003
Create Date: 2026-05-01
"""

from __future__ import annotations

from alembic import op

revision: str = "004"
down_revision: str = "003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_assessments_project_slug", "task_assessments", ["project_slug"])
    op.create_index("ix_memories_superseded_by", "memories", ["superseded_by"])


def downgrade() -> None:
    op.drop_index("ix_memories_superseded_by", table_name="memories")
    op.drop_index("ix_assessments_project_slug", table_name="task_assessments")

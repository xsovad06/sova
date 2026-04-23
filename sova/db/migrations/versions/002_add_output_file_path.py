"""Add output_file_path to task_runs.

Revision ID: 002
Revises: 001
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str = "001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("output_file_path", sa.String(500)))


def downgrade() -> None:
    op.drop_column("task_runs", "output_file_path")

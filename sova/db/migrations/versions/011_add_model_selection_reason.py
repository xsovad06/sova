"""Add model_selection_reason column to cost_records.

Tracks why a specific model was selected for each LLM invocation
(e.g., "role:triage->haiku", "complexity:moderate->sonnet").

Revision ID: 011
Revises: 010
Create Date: 2026-06-29T10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str = "010"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if _column_exists("cost_records", "model_selection_reason"):
        return

    op.add_column("cost_records", sa.Column("model_selection_reason", sa.String(200), nullable=True))


def downgrade() -> None:
    if not _column_exists("cost_records", "model_selection_reason"):
        return

    op.drop_column("cost_records", "model_selection_reason")

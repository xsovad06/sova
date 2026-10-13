"""Add reasoning_tokens column to cost_records.

Persists the Codex-only reasoning-token breakdown of output_tokens
(sova.llm.models.LLMResult.reasoning_output_tokens). Nullable: NULL means no
source for this record reported the figure (every non-Codex row, and any
pre-existing row).

Revision ID: 038
Revises: 037
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: str = "037"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("cost_records", "reasoning_tokens"):
        op.add_column("cost_records", sa.Column("reasoning_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _column_exists("cost_records", "reasoning_tokens"):
        op.drop_column("cost_records", "reasoning_tokens")

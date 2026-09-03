"""Add compression savings columns to cost_records.

Adds pre_compression_input_tokens and tokens_saved to record token savings
from Headroom prompt compression (issue #897). Both columns are nullable so
existing records and non-compressed invocations remain intact (NULL). Zero is
reserved for the case where compression ran but saved nothing.

Revision ID: 034
Revises: 033
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: str = "033"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("cost_records", "pre_compression_input_tokens"):
        op.add_column("cost_records", sa.Column("pre_compression_input_tokens", sa.Integer, nullable=True))

    if not _column_exists("cost_records", "tokens_saved"):
        op.add_column("cost_records", sa.Column("tokens_saved", sa.Integer, nullable=True))


def downgrade() -> None:
    if _column_exists("cost_records", "tokens_saved"):
        op.drop_column("cost_records", "tokens_saved")

    if _column_exists("cost_records", "pre_compression_input_tokens"):
        op.drop_column("cost_records", "pre_compression_input_tokens")

"""Add cache_read_tokens and cache_write_tokens to cost_records.

Splits the combined cache_tokens column into granular read/write
breakdowns. Both columns are nullable so existing records remain
intact. The original cache_tokens column is preserved as the
combined total for backward compatibility.

Revision ID: 028
Revises: 027
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "028"
down_revision: str = "027"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("cost_records", "cache_read_tokens"):
        op.add_column("cost_records", sa.Column("cache_read_tokens", sa.Integer, nullable=True))

    if not _column_exists("cost_records", "cache_write_tokens"):
        op.add_column("cost_records", sa.Column("cache_write_tokens", sa.Integer, nullable=True))


def downgrade() -> None:
    if _column_exists("cost_records", "cache_write_tokens"):
        op.drop_column("cost_records", "cache_write_tokens")

    if _column_exists("cost_records", "cache_read_tokens"):
        op.drop_column("cost_records", "cache_read_tokens")

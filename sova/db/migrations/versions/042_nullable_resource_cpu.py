"""Make the stored CPU readings of resource samples and summaries nullable.

A collector's first sample has no CPU baseline, and a run that ends before its second sample has no CPU
reading at all. The columns were NOT NULL, so the writer dropped the first sample (losing its valid memory,
I/O and thread data) and stored 0.0 plus ``sample_count`` 0 for an unmeasured summary, overloading the count as
an "unmeasured" flag. NULL now means "not measured", never zero, and ``sample_count`` is the true count.
Existing rows keep their values.

Revision ID: 042
Revises: 041
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: str = "041"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("resource_samples", "cpu_percent"),
    ("resource_summaries", "peak_cpu_percent"),
    ("resource_summaries", "avg_cpu_percent"),
)


def _is_nullable(table_name: str, column_name: str) -> bool | None:
    """Whether the column is nullable, None when the table or column is missing.

    A missing table is left alone in both directions: the self-healing create_all path builds it from the ORM
    model, which already declares the column nullable.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return None
    for column in inspector.get_columns(table_name):
        if column["name"] == column_name:
            return bool(column["nullable"])
    return None


# Batch mode for every change (docs/database-guidelines.md): SQLite cannot alter a column in place.
def upgrade() -> None:
    for table_name, column_name in _COLUMNS:
        if _is_nullable(table_name, column_name) is False:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.alter_column(column_name, existing_type=sa.Numeric(8, 2), nullable=True)


def downgrade() -> None:
    for table_name, column_name in _COLUMNS:
        if _is_nullable(table_name, column_name):
            # A NOT NULL column cannot hold the unmeasured rows: they read 0.0 again, as they did before.
            op.execute(sa.text(f"UPDATE {table_name} SET {column_name} = 0 WHERE {column_name} IS NULL"))  # noqa: S608
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.alter_column(column_name, existing_type=sa.Numeric(8, 2), nullable=False)

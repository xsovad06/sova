"""Add cost_source column to cost_records.

Persists the provenance of CostRecord.cost_usd (sova.llm.models.CostSource's
value: "priced"/"free_local"/"subscription"/"unknown"). Nullable so every
pre-existing row reads as NULL (legacy, pre-#1128 data), distinct from the
explicit "unknown" value a current writer sets when no trustworthy dollar
figure exists.

Revision ID: 037
Revises: 036
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: str = "036"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("cost_records", "cost_source"):
        op.add_column("cost_records", sa.Column("cost_source", sa.String(20), nullable=True))


def downgrade() -> None:
    if _column_exists("cost_records", "cost_source"):
        op.drop_column("cost_records", "cost_source")

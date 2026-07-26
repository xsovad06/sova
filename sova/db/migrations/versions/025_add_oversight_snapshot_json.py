"""Add snapshot_json column to oversight_runs table.

Stores the cross-project health snapshot collected during each oversight
agent wake cycle (#445).

Revision ID: 025
Revises: 024
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: str = "024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "oversight_runs" not in inspector.get_table_names():
        return

    columns = [c["name"] for c in inspector.get_columns("oversight_runs")]
    if "snapshot_json" in columns:
        return

    op.add_column("oversight_runs", sa.Column("snapshot_json", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("oversight_runs", "snapshot_json")

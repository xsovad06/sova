"""Add oversight_runs table for the oversight agent wake cycle tracking.

Records each wake cycle with status, duration, and optional error details.
Used by the OversightAgent background daemon (#444).

Revision ID: 022
Revises: 021
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str = "021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "oversight_runs" in inspector.get_table_names():
        return

    op.create_table(
        "oversight_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("cycle_number", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_oversight_runs_status", "oversight_runs", ["status"])
    op.create_index("ix_oversight_runs_started", "oversight_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_oversight_runs_started", table_name="oversight_runs")
    op.drop_index("ix_oversight_runs_status", table_name="oversight_runs")
    op.drop_table("oversight_runs")

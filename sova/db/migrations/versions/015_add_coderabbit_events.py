"""Add coderabbit_events table for quota tracking.

Revision ID: 015
Revises: 014
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str = "014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "coderabbit_events" not in existing:
        op.create_table(
            "coderabbit_events",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("pr_number", sa.Integer, nullable=False),
            sa.Column("event_type", sa.String(30), nullable=False),
            sa.Column("review_id", sa.String(100), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("project_slug", sa.String(100), server_default=""),
            sa.UniqueConstraint("review_id", "project_slug", name="uq_coderabbit_event_review"),
        )
        op.create_index("ix_coderabbit_events_recorded", "coderabbit_events", ["recorded_at"])
        op.create_index("ix_coderabbit_events_project", "coderabbit_events", ["project_slug"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "coderabbit_events" in existing:
        op.drop_index("ix_coderabbit_events_project", table_name="coderabbit_events")
        op.drop_index("ix_coderabbit_events_recorded", table_name="coderabbit_events")
        op.drop_table("coderabbit_events")

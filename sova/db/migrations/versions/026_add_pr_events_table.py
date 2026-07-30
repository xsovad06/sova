"""Add pr_events table for PR lifecycle metrics.

Stores append-only PR lifecycle events (opened, reviewed, approved,
merged, etc.) to compute cycle time, throughput, and review efficiency.
Unique constraint on (pr_number, repo, event_type, timestamp) ensures
idempotent backfill.

Revision ID: 026
Revises: 025
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str = "025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "pr_events" in inspector.get_table_names():
        return

    op.create_table(
        "pr_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("project_slug", sa.String(100), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "pr_number",
            "repo",
            "event_type",
            "timestamp",
            name="uq_pr_events_dedup",
        ),
    )
    op.create_index("ix_pr_events_pr_repo", "pr_events", ["pr_number", "repo"])
    op.create_index("ix_pr_events_timestamp", "pr_events", ["timestamp"])
    op.create_index("ix_pr_events_project", "pr_events", ["project_slug"])


def downgrade() -> None:
    op.drop_index("ix_pr_events_project", table_name="pr_events")
    op.drop_index("ix_pr_events_timestamp", table_name="pr_events")
    op.drop_index("ix_pr_events_pr_repo", table_name="pr_events")
    op.drop_table("pr_events")

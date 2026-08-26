"""Add feed_events table for persistent activity feed history.

Backs the chat-style cockpit timeline (issue #852) with durable history
that survives page reloads and server restarts. The in-memory ring buffer
in FeedService still drives SSE fan-out; these records enable backward
pagination (infinite scroll) of the timeline.

Revision ID: 032
Revises: 031
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: str = "031"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("feed_events"):
        return

    op.create_table(
        "feed_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("category", sa.String(64), nullable=False, server_default="system"),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_feed_events_created", "feed_events", ["created_at"])


def downgrade() -> None:
    if _table_exists("feed_events"):
        op.drop_index("ix_feed_events_created", table_name="feed_events")
        op.drop_table("feed_events")

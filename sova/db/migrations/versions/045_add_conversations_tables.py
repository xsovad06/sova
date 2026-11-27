"""Add the conversations and conversation_messages tables: the feed panel's conversation tabs (issue #1544).

Revision ID: 045
Revises: 044
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "045"
down_revision: str = "044"
branch_labels: str | None = None
depends_on: str | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # The self-healing create_all path may already have built the tables from the ORM models.
    if not _has_table("conversations"):
        op.create_table(
            "conversations",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("project_dir", sa.String(1000), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("issue_number", sa.String(50), nullable=True),
            sa.Column("runtime", sa.String(50), nullable=False),
            sa.Column("session_ref", sa.String(200), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("total_cost_usd", sa.Numeric(10, 6), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_conversations_project_status", "conversations", ["project_dir", "status"])
    if not _has_table("conversation_messages"):
        op.create_table(
            "conversation_messages",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "conversation_id",
                sa.Integer,
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("tools", sa.JSON, nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error", sa.Text, nullable=False),
            sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
        op.create_index("ix_conversation_messages_status", "conversation_messages", ["status"])
        op.create_index("ix_conversation_messages_created_at", "conversation_messages", ["created_at"])


def downgrade() -> None:
    if _has_table("conversation_messages"):
        op.drop_index("ix_conversation_messages_created_at", table_name="conversation_messages")
        op.drop_index("ix_conversation_messages_status", table_name="conversation_messages")
        op.drop_index("ix_conversation_messages_conversation_id", table_name="conversation_messages")
        op.drop_table("conversation_messages")
    if _has_table("conversations"):
        op.drop_index("ix_conversations_project_status", table_name="conversations")
        op.drop_table("conversations")

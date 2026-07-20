"""Add action_feedback table for the dual-evaluation PR state experiment.

Stores both the deterministic system's suggested action and the LLM's suggestion
for every PR where they disagree, along with the user's choice. Used to accumulate
ground-truth data to improve the deterministic model over time.

Revision ID: 021
Revises: 020
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: str = "020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "action_feedback" in inspector.get_table_names():
        return

    op.create_table(
        "action_feedback",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("issue_number", sa.String(50), nullable=True),
        sa.Column("project_slug", sa.String(100), nullable=False, server_default=""),
        sa.Column("deterministic_state", sa.String(50), nullable=False),
        sa.Column("deterministic_action_id", sa.String(50), nullable=False),
        sa.Column("llm_action_id", sa.String(50), nullable=False),
        sa.Column("llm_reasoning", sa.Text, nullable=False, server_default=""),
        sa.Column("user_choice", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_action_feedback_pr", "action_feedback", ["pr_number"])
    op.create_index(
        "ix_action_feedback_project_state",
        "action_feedback",
        ["project_slug", "deterministic_state"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_feedback_project_state", table_name="action_feedback")
    op.drop_index("ix_action_feedback_pr", table_name="action_feedback")
    op.drop_table("action_feedback")

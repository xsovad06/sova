"""Add supervisor_decisions table for daemon decision logging.

Append-only log of supervisor polling decisions: progression actions,
health checks, PR throttle status, etc. Time-based retention purge
keeps the table bounded.

Revision ID: 023
Revises: 022
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str = "022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "supervisor_decisions" in inspector.get_table_names():
        return

    op.create_table(
        "supervisor_decisions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_slug", sa.String(100), nullable=False, server_default=""),
        sa.Column("component", sa.String(50), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("issue_number", sa.String(50), nullable=True),
        sa.Column("action", sa.String(50), nullable=False, server_default=""),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_supervisor_decisions_project", "supervisor_decisions", ["project_slug"])
    op.create_index("ix_supervisor_decisions_component", "supervisor_decisions", ["component"])
    op.create_index("ix_supervisor_decisions_created", "supervisor_decisions", ["created_at"])
    op.create_index("ix_supervisor_decisions_event_type", "supervisor_decisions", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_supervisor_decisions_event_type", table_name="supervisor_decisions")
    op.drop_index("ix_supervisor_decisions_created", table_name="supervisor_decisions")
    op.drop_index("ix_supervisor_decisions_component", table_name="supervisor_decisions")
    op.drop_index("ix_supervisor_decisions_project", table_name="supervisor_decisions")
    op.drop_table("supervisor_decisions")

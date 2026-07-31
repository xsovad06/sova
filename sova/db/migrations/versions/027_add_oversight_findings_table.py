"""Add oversight_findings table for analysis results.

Stores structured findings produced by the oversight agent's LLM analysis
step. Each finding is linked to an OversightRun via run_id (FK to
oversight_runs.id, String(36) UUID).

Revision ID: 027
Revises: 026
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: str = "026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "oversight_findings" in inspector.get_table_names():
        return

    op.create_table(
        "oversight_findings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("oversight_runs.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False, server_default="global"),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("recommendation", sa.Text, nullable=False, server_default=""),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="0.5"),
        sa.Column("project_slug", sa.String(100), nullable=False, server_default=""),
        sa.Column("dismissed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("github_issue_number", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_oversight_findings_run", "oversight_findings", ["run_id"])
    op.create_index("ix_oversight_findings_title", "oversight_findings", ["title"])
    op.create_index("ix_oversight_findings_scope", "oversight_findings", ["scope"])
    op.create_index("ix_oversight_findings_created", "oversight_findings", ["created_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "oversight_findings" not in inspector.get_table_names():
        return

    op.drop_index("ix_oversight_findings_created", table_name="oversight_findings")
    op.drop_index("ix_oversight_findings_scope", table_name="oversight_findings")
    op.drop_index("ix_oversight_findings_title", table_name="oversight_findings")
    op.drop_index("ix_oversight_findings_run", table_name="oversight_findings")
    op.drop_table("oversight_findings")

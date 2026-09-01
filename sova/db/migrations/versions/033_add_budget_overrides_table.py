"""Add budget_overrides table for per-issue budget override audit trail.

Records each user-confirmed budget override with spend/limit context,
enabling auditability and feed event visibility (issue #885).

Revision ID: 033
Revises: 032
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: str = "032"
branch_labels: str | None = None
depends_on: str | None = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("budget_overrides"):
        return

    op.create_table(
        "budget_overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("issue_number", sa.String(50), nullable=False),
        sa.Column("task_run_id", sa.Integer, sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("spend_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("limit_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_budget_overrides_issue", "budget_overrides", ["issue_number"])
    op.create_index("ix_budget_overrides_created", "budget_overrides", ["created_at"])


def downgrade() -> None:
    if _table_exists("budget_overrides"):
        op.drop_index("ix_budget_overrides_created", table_name="budget_overrides")
        op.drop_index("ix_budget_overrides_issue", table_name="budget_overrides")
        op.drop_table("budget_overrides")

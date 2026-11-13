"""Add the pr_retros table: which recorded rules each merged PR's review findings repeat.

One row per merged PR, written by ``sova retro record`` and ``sova retro backfill`` (issue #744).

Revision ID: 043
Revises: 042
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "043"
down_revision: str = "042"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "pr_retros"


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    # The self-healing create_all path may already have built the table from the ORM model.
    if _table_exists():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_rounds", sa.Integer, nullable=False),
        sa.Column("address_cycles", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("classify_cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("classify_model", sa.String(100), nullable=False),
        sa.Column("findings_by_severity", sa.JSON, nullable=False),
        sa.Column("kinds_digest", sa.String(64), nullable=False),
        sa.Column("kinds_count", sa.Integer, nullable=False),
        sa.Column("findings", sa.JSON, nullable=False),
        sa.UniqueConstraint("pr_number", name="uq_pr_retros_pr_number"),
    )
    op.create_index("ix_pr_retros_merged_at", _TABLE, ["merged_at"])


def downgrade() -> None:
    if _table_exists():
        op.drop_index("ix_pr_retros_merged_at", table_name=_TABLE)
        op.drop_table(_TABLE)

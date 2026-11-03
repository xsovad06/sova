"""Add cost_records.num_turns and task_runs.phase_usage.

num_turns persists the Claude CLI's per-session turn count
(sova.llm.models.LLMResult.num_turns); phase_usage persists the per-phase
turn, token and time breakdown of a command:* run. Both nullable: NULL means
not reported (Codex, API providers, non-command runs, and every pre-existing
row), never zero. Issue #1336.

Revision ID: 041
Revises: 040
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: str = "040"
branch_labels: str | None = None
depends_on: str | None = None


def _column_state(table_name: str, column_name: str) -> bool | None:
    """True or False for whether the column exists, None when the table itself is missing.

    A missing table is left alone in both directions: the self-healing
    create_all path builds it from the ORM model, which already carries the
    column, so adding it here would fail and dropping it is moot.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return None
    return column_name in [c["name"] for c in inspector.get_columns(table_name)]


# Batch mode for every change (docs/database-guidelines.md): a SQLite build
# without native DROP COLUMN would otherwise fail the downgrade.
def upgrade() -> None:
    if _column_state("cost_records", "num_turns") is False:
        with op.batch_alter_table("cost_records") as batch_op:
            batch_op.add_column(sa.Column("num_turns", sa.Integer(), nullable=True))
    if _column_state("task_runs", "phase_usage") is False:
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.add_column(sa.Column("phase_usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _column_state("task_runs", "phase_usage"):
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.drop_column("phase_usage")
    if _column_state("cost_records", "num_turns"):
        with op.batch_alter_table("cost_records") as batch_op:
            batch_op.drop_column("num_turns")

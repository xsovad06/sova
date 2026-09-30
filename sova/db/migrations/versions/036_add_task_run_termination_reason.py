"""Add termination_reason column to task_runs.

Records why a run's process actually stopped: the cause from a locally
recorded TerminationRecord for a deliberate stop() call (e.g. "manual_stop",
a watchdog anomaly signal name, "memory_pressure"), or "external_signal" for
a signal-shaped exit with no local record (issue #978). Nullable so every
pre-existing row (and a normal, non-signal exit) reads as NULL, which must
always be treated as "unknown", never as "external".

Revision ID: 036
Revises: 035
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: str = "035"
branch_labels: str | None = None
depends_on: str | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("task_runs", "termination_reason"):
        op.add_column("task_runs", sa.Column("termination_reason", sa.String(50), nullable=True))


def downgrade() -> None:
    if _column_exists("task_runs", "termination_reason"):
        op.drop_column("task_runs", "termination_reason")

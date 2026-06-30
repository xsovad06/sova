"""Add missing FK indexes and composite lifecycle_phases index.

Adds indexes on:
- task_runs.lifecycle_id
- task_runs.workflow_definition_id
- lifecycle_phases.task_run_id
- lifecycle_phases(lifecycle_id, phase) composite (replaces ix_lifecycle_phases_lifecycle)

Revision ID: 012
Revises: 011
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str = "011"
branch_labels: str | None = None
depends_on: str | None = None


def _get_index_names(table_name: str) -> set[str]:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    tr_indexes = _get_index_names("task_runs")
    lp_indexes = _get_index_names("lifecycle_phases")

    if "ix_task_runs_lifecycle_id" not in tr_indexes:
        op.create_index("ix_task_runs_lifecycle_id", "task_runs", ["lifecycle_id"])

    if "ix_task_runs_workflow_definition_id" not in tr_indexes:
        op.create_index("ix_task_runs_workflow_definition_id", "task_runs", ["workflow_definition_id"])

    if "ix_lifecycle_phases_task_run_id" not in lp_indexes:
        op.create_index("ix_lifecycle_phases_task_run_id", "lifecycle_phases", ["task_run_id"])

    if "ix_lifecycle_phases_lifecycle_phase" not in lp_indexes:
        op.create_index("ix_lifecycle_phases_lifecycle_phase", "lifecycle_phases", ["lifecycle_id", "phase"])

    if "ix_lifecycle_phases_lifecycle" in lp_indexes:
        op.drop_index("ix_lifecycle_phases_lifecycle", table_name="lifecycle_phases")


def downgrade() -> None:
    tr_indexes = _get_index_names("task_runs")
    lp_indexes = _get_index_names("lifecycle_phases")

    if "ix_lifecycle_phases_lifecycle" not in lp_indexes:
        op.create_index("ix_lifecycle_phases_lifecycle", "lifecycle_phases", ["lifecycle_id"])

    if "ix_lifecycle_phases_lifecycle_phase" in lp_indexes:
        op.drop_index("ix_lifecycle_phases_lifecycle_phase", table_name="lifecycle_phases")

    if "ix_lifecycle_phases_task_run_id" in lp_indexes:
        op.drop_index("ix_lifecycle_phases_task_run_id", table_name="lifecycle_phases")

    if "ix_task_runs_workflow_definition_id" in tr_indexes:
        op.drop_index("ix_task_runs_workflow_definition_id", table_name="task_runs")

    if "ix_task_runs_lifecycle_id" in tr_indexes:
        op.drop_index("ix_task_runs_lifecycle_id", table_name="task_runs")

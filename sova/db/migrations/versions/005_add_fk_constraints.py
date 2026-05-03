"""Add foreign key constraints and fix confidence precision.

Revision ID: 005
Revises: 004
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str = "004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("step_executions") as batch_op:
        batch_op.alter_column("task_run_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("failure_records") as batch_op:
        batch_op.alter_column("task_run_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("task_assessments") as batch_op:
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Numeric(3, 2),
            type_=sa.Numeric(4, 3),
        )


def downgrade() -> None:
    with op.batch_alter_table("task_assessments") as batch_op:
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Numeric(4, 3),
            type_=sa.Numeric(3, 2),
        )

"""Add telemetry_events table for remote fleet telemetry ingestion.

Stores run summaries pushed by remote SOVA instances to the hub.
Unique constraint on (machine_id, run_id) ensures idempotent ingestion.

Revision ID: 024
Revises: 023
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: str = "023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "telemetry_events" in inspector.get_table_names():
        return

    op.create_table(
        "telemetry_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("machine_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("project_slug", sa.String(100), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("issue_number", sa.String(50), nullable=True),
        sa.Column("pr_number", sa.Integer, nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("step_outcomes", sa.JSON, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("machine_id", "run_id", name="uq_telemetry_machine_run"),
    )
    op.create_index("ix_telemetry_events_machine", "telemetry_events", ["machine_id"])
    op.create_index("ix_telemetry_events_project", "telemetry_events", ["project_slug"])
    op.create_index("ix_telemetry_events_received", "telemetry_events", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_telemetry_events_received", table_name="telemetry_events")
    op.drop_index("ix_telemetry_events_project", table_name="telemetry_events")
    op.drop_index("ix_telemetry_events_machine", table_name="telemetry_events")
    op.drop_table("telemetry_events")

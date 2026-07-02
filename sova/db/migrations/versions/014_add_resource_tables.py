"""Add resource_samples and resource_summaries tables.

Revision ID: 014
Revises: 013
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str = "013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "resource_samples" not in existing:
        op.create_table(
            "resource_samples",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "task_run_id",
                sa.Integer,
                sa.ForeignKey("task_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("cpu_percent", sa.Numeric(8, 2), nullable=False),
            sa.Column("memory_rss_bytes", sa.Integer, nullable=False),
            sa.Column("memory_vms_bytes", sa.Integer, nullable=False),
            sa.Column("io_read_bytes", sa.Integer, nullable=True),
            sa.Column("io_write_bytes", sa.Integer, nullable=True),
            sa.Column("num_children", sa.Integer, nullable=False, server_default="0"),
            sa.Column("num_threads", sa.Integer, nullable=False, server_default="0"),
        )
        op.create_index("ix_resource_samples_run_time", "resource_samples", ["task_run_id", "sampled_at"])

    if "resource_summaries" not in existing:
        op.create_table(
            "resource_summaries",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "task_run_id",
                sa.Integer,
                sa.ForeignKey("task_runs.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("sample_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("peak_cpu_percent", sa.Numeric(8, 2), nullable=False, server_default="0"),
            sa.Column("avg_cpu_percent", sa.Numeric(8, 2), nullable=False, server_default="0"),
            sa.Column("peak_memory_rss_bytes", sa.Integer, nullable=False, server_default="0"),
            sa.Column("peak_memory_vms_bytes", sa.Integer, nullable=False, server_default="0"),
            sa.Column("total_io_read_bytes", sa.Integer, nullable=True),
            sa.Column("total_io_write_bytes", sa.Integer, nullable=True),
            sa.Column("peak_num_threads", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "resource_summaries" in existing:
        op.drop_table("resource_summaries")
    if "resource_samples" in existing:
        op.drop_index("ix_resource_samples_run_time", table_name="resource_samples")
        op.drop_table("resource_samples")

"""Add memory_edges table for knowledge graph relationships.

Revision ID: 013
Revises: 012
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str = "012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "memory_edges" in inspector.get_table_names():
        return

    op.create_table(
        "memory_edges",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", sa.Integer, sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation", sa.String(30), nullable=False, server_default="relates_to"),
        sa.Column("weight", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "target_id", "relation", name="uq_memory_edge"),
        sa.CheckConstraint(
            "relation IN ('relates_to', 'refines', 'depends_on', 'supersedes', 'contradicts')",
            name="ck_memory_edge_relation",
        ),
        sa.CheckConstraint("weight >= 0.0 AND weight <= 1.0", name="ck_memory_edge_weight"),
    )
    op.create_index("ix_memory_edges_source", "memory_edges", ["source_id"])
    op.create_index("ix_memory_edges_target", "memory_edges", ["target_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "memory_edges" not in inspector.get_table_names():
        return

    op.drop_index("ix_memory_edges_target", table_name="memory_edges")
    op.drop_index("ix_memory_edges_source", table_name="memory_edges")
    op.drop_table("memory_edges")

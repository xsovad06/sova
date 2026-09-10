"""Repair ORM schema drift: memory lifecycle columns and missing indexes.

Five columns were added to the Memory ORM model without a migration: embedding
(2026-06-30, semantic search) and retrieval_count, last_retrieved_at, archived,
health_score (2026-07-02, memory lifecycle management). No migration has touched
the memories table since 001_initial_schema, so every database built by replaying
the chain lacks all five. SQLAlchemy emits every mapped column in its SELECT list,
so the whole memory subsystem (dashboard /api/memory, sova memory search, sova
memory health) failed with "no such column: memories.embedding".

Also creates four indexes declared in models.py but never present in the chain:
ix_memories_archived, plus the auto-named task_run_id indexes on step_executions,
failure_records and cost_records (declared via index=True, so no migration ever
emitted them). Those three tables carry no index on task_run_id at all today, and
every per-run dashboard query filters on it.

The two task_assessments indexes the model declares (ix_assessments_issue and
ix_assessments_suitability) are deliberately NOT created here: 001_initial_schema
already indexes the same columns as ix_task_assessments_issue and
ix_task_assessments_suitability. That is name-only drift, so creating the model's
names would add a second redundant index over each column.

Revision ID: 035
Revises: 034
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: str = "034"
branch_labels: str | None = None
depends_on: str | None = None

_MISSING_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_memories_archived", "memories", ["archived"]),
    ("ix_step_executions_task_run_id", "step_executions", ["task_run_id"]),
    ("ix_failure_records_task_run_id", "failure_records", ["task_run_id"]),
    ("ix_cost_records_task_run_id", "cost_records", ["task_run_id"]),
)


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    indexes = [i["name"] for i in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    if not _column_exists("memories", "embedding"):
        op.add_column("memories", sa.Column("embedding", sa.JSON, nullable=True))

    # NOT NULL with a server_default: SQLite requires a default to backfill existing rows.
    if not _column_exists("memories", "retrieval_count"):
        op.add_column(
            "memories",
            sa.Column("retrieval_count", sa.Integer, nullable=False, server_default="0"),
        )

    if not _column_exists("memories", "last_retrieved_at"):
        op.add_column("memories", sa.Column("last_retrieved_at", sa.DateTime(timezone=True), nullable=True))

    if not _column_exists("memories", "archived"):
        op.add_column(
            "memories",
            sa.Column("archived", sa.Boolean, nullable=False, server_default="0"),
        )

    if not _column_exists("memories", "health_score"):
        op.add_column("memories", sa.Column("health_score", sa.Numeric(5, 4), nullable=True))

    for index_name, table_name, columns in _MISSING_INDEXES:
        if not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    for index_name, table_name, _columns in reversed(_MISSING_INDEXES):
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    for column_name in ("health_score", "archived", "last_retrieved_at", "retrieval_count", "embedding"):
        if _column_exists("memories", column_name):
            op.drop_column("memories", column_name)

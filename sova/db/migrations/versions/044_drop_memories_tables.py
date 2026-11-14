"""Drop the memories and memory_edges tables, exporting any rows first.

The memory subsystem (issue #1464) was removed: nothing reads or writes these tables any more.
Before dropping, every row of both tables is written to one JSON file,
``<project>/.claude/memories-export-<UTC timestamp>.json``, with the keys ``memories`` and
``memory_edges``. The file is only written when at least one table holds a row, is never
overwritten (exclusive create), is created owner-only (mode 0o600), and is written and flushed before
anything is dropped: if the write fails the migration aborts with both tables intact. Its absolute path
and the row counts are logged at WARNING. On a database shared
by several projects, the first process to run the migration receives every project's rows (told apart by
the ``repo`` column). The export is not re-imported by anything.

Serialization: datetimes become ISO strings, ``Decimal`` becomes a string, bytes become base64
strings and any other non-JSON value becomes ``str(value)``. The ``memories.embedding`` column is a
JSON list of floats, so it is exported as that list.

The migration reads the tables by reflection and defines nothing from the removed ORM models. The
downgrade recreates both tables empty (columns, indexes and foreign keys as the earlier revisions
left them: 001, 004, 013 and 035); it does not re-import an export file.

Revision ID: 044
Revises: 043
Create Date: 2026-11-13
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op

from sova.utils.logging import get_logger

revision: str = "044"
down_revision: str = "043"
branch_labels: str | None = None
depends_on: str | None = None

log = get_logger(component="db.migration.044")

_MEMORIES = "memories"
_EDGES = "memory_edges"
_EXPORT_DIR_ATTRIBUTE = "memories_export_dir"
_MAX_NAME_ATTEMPTS = 100


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _export_dir(bind: sa.engine.Connection) -> tuple[Path, str]:
    """The directory the export goes to, and why that one.

    ``_run_migrations()`` passes the project's ``.claude/`` through the Alembic config (reason
    ``project``). A run with no project (the bare ``alembic`` CLI, or a server with no project
    context) uses a SQLite file's own directory (``database_file``); any other database, such as a
    shared PostgreSQL one, falls back to the db layer's stable default ``~/.config/sova``
    (``fallback_state_dir``), never the current directory.
    """
    configured = op.get_context().config.attributes.get(_EXPORT_DIR_ATTRIBUTE)
    if configured is not None:
        return Path(configured), "project"
    database = bind.engine.url.database
    if bind.dialect.name == "sqlite" and database and database != ":memory:":
        return Path(database).parent, "database_file"
    return Path.home() / ".config" / "sova", "fallback_state_dir"


def _read_rows(bind: sa.engine.Connection, table_name: str) -> list[dict[str, Any]]:
    if not sa.inspect(bind).has_table(table_name):
        return []
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    order_by = [table.c.id] if "id" in table.c else []
    return [dict(row) for row in bind.execute(sa.select(table).order_by(*order_by)).mappings()]


def _write_export(directory: Path, payload: dict[str, list[dict[str, Any]]]) -> Path:
    """Write the payload to a new file in ``directory`` and return its path.

    Exclusive creation means an existing export is never overwritten; a name clash (two runs in
    the same microsecond) moves on to the next numbered suffix.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for attempt in range(_MAX_NAME_ATTEMPTS):
        suffix = f"-{attempt}" if attempt else ""
        path = directory / f"memories-export-{stamp}{suffix}.json"
        try:
            # Owner-only (0o600): the rows hold free-text agent output. O_EXCL keeps the never-overwrite guarantee.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        handle = os.fdopen(fd, "w", encoding="utf-8")
        try:
            with handle:
                json.dump(payload, handle, default=_json_default, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise FileExistsError(f"no free memories-export file name in {directory}")


def upgrade() -> None:
    bind = op.get_bind()
    payload = {_MEMORIES: _read_rows(bind, _MEMORIES), _EDGES: _read_rows(bind, _EDGES)}

    if any(payload.values()):
        directory, location = _export_dir(bind)
        path = _write_export(directory, payload)
        # On a database shared by several projects the first process to migrate receives every project's rows
        # (the `repo` column tells them apart); the file is the only copy of the dropped data.
        log.warning(
            "db.memories_exported",
            path=str(path.resolve()),
            location=location,
            project_local=location == "project",
            memories=len(payload[_MEMORIES]),
            memory_edges=len(payload[_EDGES]),
            note="only copy of the dropped memory tables; keep it out of version control",
        )

    inspector = sa.inspect(bind)
    # memory_edges first: it references memories.
    for table_name in (_EDGES, _MEMORIES):
        if inspector.has_table(table_name):
            op.drop_table(table_name)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_MEMORIES):
        op.create_table(
            _MEMORIES,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("category", sa.String(50), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("tags", sa.String(500), server_default=""),
            sa.Column("repo", sa.String(200), server_default=""),
            sa.Column("issue_number", sa.String(50)),
            sa.Column("tier", sa.String(20), server_default="project"),
            sa.Column("superseded_by", sa.Integer),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("embedding", sa.JSON, nullable=True),
            sa.Column("retrieval_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("last_retrieved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("archived", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("health_score", sa.Numeric(5, 4), nullable=True),
        )
        op.create_index("ix_memories_category", _MEMORIES, ["category"])
        op.create_index("ix_memories_tags", _MEMORIES, ["tags"])
        op.create_index("ix_memories_superseded_by", _MEMORIES, ["superseded_by"])
        op.create_index("ix_memories_archived", _MEMORIES, ["archived"])

    if not inspector.has_table(_EDGES):
        op.create_table(
            _EDGES,
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
        op.create_index("ix_memory_edges_source", _EDGES, ["source_id"])
        op.create_index("ix_memory_edges_target", _EDGES, ["target_id"])

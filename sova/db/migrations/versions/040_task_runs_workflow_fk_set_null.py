"""Add ondelete=SET NULL to task_runs.workflow_definition_id FK.

With PRAGMA foreign_keys=ON (issue #1205), deleting a workflow_definitions row
that a task_runs row still references now raises IntegrityError instead of
silently leaving a dangling reference. The column is already nullable, so
SET NULL preserves the audit trail (the TaskRun row survives) rather than
blocking the delete.

Revision ID: 040
Revises: 039
Create Date: 2026-10-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "040"
down_revision: str = "039"
branch_labels: str | None = None
depends_on: str | None = None

_FK_NAME = "fk_task_runs_workflow_definition"
_FK_COLUMN = "workflow_definition_id"

# Only used to assign a deterministic name to an *unnamed* FK reflected off a
# `Base.metadata.create_all()`-built table (no naming_convention on Base, so
# SQLite stores such a constraint anonymously). A real, already-named
# constraint keeps its actual name regardless of this convention.
_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    return table_name in sa.inspect(conn).get_table_names()


def _find_fk(table_name: str, column: str) -> dict | None:
    conn = op.get_bind()
    for fk in sa.inspect(conn).get_foreign_keys(table_name):
        if fk.get("constrained_columns") == [column]:
            return fk
    return None


def _drop_name_for(fk: dict, table_name: str, column: str) -> str:
    """Return the constraint name to pass to drop_constraint.

    A constraint reflected with a real name uses it as-is. An unnamed one
    (created by create_all with no naming_convention on Base.metadata) is
    given the deterministic name _NAMING_CONVENTION assigns on reflection, via
    the `naming_convention=` kwarg on batch_alter_table below: SQLAlchemy
    reflection cannot name an anonymous SQLite FK itself, but Alembic's batch
    mode can be told to apply a convention to it before operating on it.
    """
    if fk.get("name"):
        return fk["name"]
    return _NAMING_CONVENTION["fk"] % {
        "table_name": table_name,
        "column_0_name": column,
        "referred_table_name": fk["referred_table"],
    }


def upgrade() -> None:
    if not _table_exists("task_runs"):
        return

    fk = _find_fk("task_runs", _FK_COLUMN)
    if fk is not None and (fk.get("options") or {}).get("ondelete") == "SET NULL":
        return  # Already applied (e.g. a fresh create_all build with the ORM's own ondelete).

    with op.batch_alter_table("task_runs", naming_convention=_NAMING_CONVENTION) as batch_op:
        if fk is not None:
            batch_op.drop_constraint(_drop_name_for(fk, "task_runs", _FK_COLUMN), type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "workflow_definitions",
            [_FK_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if not _table_exists("task_runs"):
        return

    fk = _find_fk("task_runs", _FK_COLUMN)
    if fk is None:
        return

    with op.batch_alter_table("task_runs", naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(_drop_name_for(fk, "task_runs", _FK_COLUMN), type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "workflow_definitions",
            [_FK_COLUMN],
            ["id"],
        )

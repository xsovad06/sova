---
name: database-patterns
description: "SOVA database conventions: async SQLAlchemy 2.0, ORM models, session management, migrations, JSON column gotchas. Auto-activates when working on sova/db/ models, session code, or Alembic migrations."
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# SOVA Database Patterns

When working on `sova/db/`, ORM models, or migrations, follow these conventions. Reference: `docs/database-guidelines.md`.

## Session Management

Always use the async context manager:
```python
async with await get_session() as session:
    result = await session.execute(select(TaskRun))
```
Never acquire sessions without `async with`: it leaks on exception. All factories use `expire_on_commit=False`.

## ORM Model Conventions (sova/db/models.py)

- Type-annotated columns: `Mapped[str]` with `mapped_column(String(50))`
- Money: `Numeric(10, 6)` with `Decimal("0")` default
- Timestamps: `DateTime(timezone=True)` with `lambda: datetime.now(timezone.utc)`
- FK constants: `_FK_TASK_RUNS_ID = "task_runs.id"`
- Indexes in `__table_args__`: `Index("ix_{table}_{column}", "column")`
- Input normalization: `@validates` decorator (e.g., strip `#` from issue numbers)

## JSON Column NULL Gotcha

SQLAlchemy `JSON` defaults to `none_as_null=False`. Python `None` becomes JSON `"null"`, not SQL `NULL`.
- `TaskRun.handoff_json.isnot(None)` only catches SQL NULLs
- Use `if not handoff:` to catch both `None` and `{}`

## Terminal Status Sets

```python
_TERMINAL = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.REJECTED})         # state machine
TASK_RUN_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted"})  # DB queries
```
Use `TASK_RUN_TERMINAL` for DB queries. Always guard finalization:
```python
if task_run.status in TASK_RUN_TERMINAL:
    return  # don't overwrite, but still update cost
```

## Migrations

- Always use `batch_alter_table` (SQLite requires it)
- Use idempotent helpers: `_column_exists()`, `_table_exists()`, `_index_exists()`
- Sequential numbering (`001`-`008`), not Alembic UUIDs
- Engine disposal after migration for file-backed SQLite (stale schema cache)
- Self-healing: if Alembic fails, drops corrupted `alembic_version`, runs `create_all` + stamps head

## Multi-Project

`get_session(project_dir=...)` routes to per-project engine. Each project gets its own `.Codex/sova.db`.

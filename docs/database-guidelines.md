# Database Guidelines

ORM conventions, session management, and migration patterns for SOVA's async SQLAlchemy stack.

## Stack

- **ORM**: SQLAlchemy 2.0 async (`DeclarativeBase`, `Mapped`, `mapped_column`)
- **Async driver**: `aiosqlite` (default), `asyncpg` (PostgreSQL)
- **Migrations**: Alembic with `render_as_batch=True` (required for SQLite `ALTER TABLE`)
- **Key files**: `sova/db/models.py`, `sova/db/session.py`, `sova/db/migrations/`

## Session Management

Always acquire sessions with the async context manager pattern:

```python
# CORRECT: auto-closes on exit
async with await get_session() as session:
    result = await session.execute(select(TaskRun))

# WRONG: leaks sessions on exception
session = await get_session()
result = await session.execute(select(TaskRun))
await session.close()
```

All session factories use `expire_on_commit=False` so attributes remain accessible after commit without re-querying.

For multi-project mode, pass `project_dir` to route to the correct DB:

```python
async with await get_session(project_dir=project_dir) as session:
    ...
```

## ORM Model Conventions

10 models in `sova/db/models.py`: `TaskRun`, `StepExecution`, `FailureRecord`, `CostRecord`, `Memory`, `TaskAssessmentRecord`, `IssueLifecycle`, `LifecyclePhaseRecord`, `WorkflowDefinition`, `CommandContract`.

| Convention | Example |
|---|---|
| Type-annotated columns | `Mapped[str]` with `mapped_column(String(50))` |
| Decimal for money | `Numeric(10, 6)` with `Decimal("0")` default |
| UTC timestamps | `DateTime(timezone=True)` with `lambda: datetime.now(timezone.utc)` |
| `onupdate` for modified-at | `onupdate=lambda: datetime.now(timezone.utc)` |
| FK constant | `_FK_TASK_RUNS_ID = "task_runs.id"` |
| Indexes via `__table_args__` | `Index("ix_task_runs_issue", "issue_number")` |
| Input normalization | `@validates("issue_number")` strips `#` prefix |
| JSON columns | `Mapped[dict | None] = mapped_column(JSON)` |

## JSON Column NULL Gotcha

SQLAlchemy's `JSON` type defaults to `none_as_null=False`. Python `None` is stored as JSON text `"null"`, not SQL `NULL`.

```python
# This query only excludes true SQL NULLs (pre-migration or raw SQL rows):
TaskRun.handoff_json.isnot(None)
# It does NOT exclude ORM rows where handoff_json=None

# Use `if not handoff:` to catch both None and {}
# Use `if handoff is None:` only for NULL/json-null specifically
```

## Terminal Status Sets

Two related frozensets in `sova/core/state.py`:

```python
# State machine terminals (3 states)
_TERMINAL = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.REJECTED})

# DB query terminals (4 states, includes "interrupted")
TASK_RUN_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted"})
```

Use `TASK_RUN_TERMINAL` for DB queries, `_TERMINAL` for state machine transition checks. "interrupted" is set by `recover_stale_runs()` for dead-PID cleanup.

## Idempotent Finalization

Multiple codepaths can finalize a TaskRun. Always guard:

```python
if task_run.status in TASK_RUN_TERMINAL:
    return  # Don't overwrite status, but still update cost
task_run.status = status
```

## Migration System

### create_all vs Alembic

| Context | Method | Why |
|---|---|---|
| **Tests** | `init_db(run_migrations=False)` using `create_all` | In-memory DBs; dispose destroys data |
| **Production** | `init_db(run_migrations=True)` using Alembic | Adds columns to existing tables |

`create_all` only creates missing tables. It never adds columns to existing ones. Adding a column without a migration causes `OperationalError: no such column`.

### SQLite WAL mode and busy timeout

Before running migrations, `init_db()` calls `_enable_sqlite_wal(engine)` which runs
`PRAGMA journal_mode=WAL`. WAL mode persists in the DB file after the first set, so all
subsequent connections automatically use WAL without re-running the PRAGMA.

All SQLite engines are also created with `connect_args={"check_same_thread": False, "timeout": 30}`.
The `timeout` maps to `sqlite3.connect(timeout=30)` (Python's busy-wait duration in seconds).
This prevents instant `SQLITE_BUSY` failures when the old and new uvicorn workers briefly
overlap during a `--reload` restart. `PRAGMA busy_timeout` is redundant with `connect_args`
timeout; only the latter is needed since it applies to all pooled connections.

### Engine disposal after migrations

`_run_migrations()` returns `bool`: `True` if DDL was executed (Alembic ran at least one
migration), `False` if the DB was already at the head revision (fast path). `init_db()` only
calls `engine.dispose()` and `_backup_db()` when `True`. For no-op restarts (DB already at
head), neither disposal nor backup runs, saving one connection round-trip (~300 ms).

### Migration conventions

1. **Use `batch_alter_table`** for all SQLite DDL (env.py has `render_as_batch=True`)
2. **Idempotent checks**: use `_column_exists()`, `_table_exists()`, `_index_exists()` helpers (defined in migrations 006, 008)
3. **Sequential numbering**: `001` through `018` (not Alembic UUIDs)
4. **Pre-migration backup**: `_backup_db()` copies `.db` to `.db.bak`, only when DDL ran
5. **Self-healing fallback**: if Alembic fails, drops corrupted `alembic_version`, runs `create_all` + stamps at head

### The five alembic_version cases

Handled in `_run_migrations()` (`sova/db/session.py`):

1. **Fresh DB** (no tables): run all migrations from scratch
2. **Pre-Alembic DB** (tables exist, no `alembic_version`): stamp at current head
3. **Already at head** (version == `_get_alembic_head()`): return `False` immediately; skip upgrade, dispose, and backup
4. **Behind head** (version < head): upgrade to head; dispose pool; backup DB
5. **Empty/corrupted `alembic_version`**: `DROP TABLE` then treat as case 1

## Multi-Project DB Support

`init_db_for_project()` caches per-project engines in `_engines: dict[str, tuple]` keyed by URL. `get_session(project_dir=...)` routes to the correct engine. Each project gets its own `.claude/sova.db` file.

## Test Fixtures

```python
@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)
```

In-memory SQLite gives each test a fresh DB. Always `close_db()` in teardown.

## Index Naming

All indexes follow `ix_{table}_{column}` pattern: `ix_task_runs_issue`, `ix_memories_category`, `ix_assessments_project_slug`. Single-column indexes cover current query patterns.

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
# CORRECT -- auto-closes on exit
async with await get_session() as session:
    result = await session.execute(select(TaskRun))

# WRONG -- leaks sessions on exception
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

# DB query terminals (4 states -- includes "interrupted")
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

### Adding a model column always requires a migration

This rule has been broken twice, and the failure is silent for months. Five columns were
added to the `Memory` model (`embedding` on 2026-06-30, then `retrieval_count`,
`last_retrieved_at`, `archived` and `health_score` on 2026-07-02) with no migration in
either commit. Consequences, all of which repeat for any column added this way:

1. **Every existing database breaks.** SQLAlchemy names every mapped column in its SELECT
   list, so one absent column takes down every query against that table. The whole memory
   subsystem (dashboard `/api/memory`, `sova memory search`, `sova memory health`) was dead
   for two months.
2. **New installs break too.** Production always replays the chain, so a column that exists
   only in the model is missing from a freshly created database as well.
3. **It cannot self-repair.** `alembic_version` records which revisions ran, not what the
   schema looks like, so a drifted DB is stamped at head and case 3 below skips the upgrade.
   The self-healing fallback does not help either: it fires only when Alembic raises, and
   `create_all` cannot alter an existing table.
4. **Tests cannot see it.** They build their schema with `create_all`, which reflects the
   model perfectly, so ORM and migration chain diverge invisibly.

`TestSchemaDriftGuard` in `tests/test_db.py` is the backstop: it replays the migrations onto
an empty database and asserts the result contains every ORM table, column, and index. The
index check matches on the column set rather than the index name, because
`001_initial_schema` created two `task_assessments` indexes under names the model later
diverged from (`ix_task_assessments_issue` versus the model's `ix_assessments_issue`), which
is harmless name drift and must not be "fixed" by creating a second index over the same
column. Migration `035_repair_schema_drift` repaired the original damage.

Note that the guard deliberately checks names, not types: a full `compare_metadata()` diff
reports 65 further entries of pre-existing cosmetic drift (51 nullability mismatches, 5
foreign keys SQLite reflects differently, 4 index names, 3 VARCHAR lengths) whose repair
would require batch-mode rebuilds of roughly 16 tables holding live run history. That
cleanup is deliberately deferred to issue #1008, and the guard is scoped to stay green
without it.

### SQLite WAL mode, synchronous, and busy timeout

Before running migrations, `init_db()` calls `_enable_sqlite_wal(engine)` which runs
`PRAGMA journal_mode=WAL`. WAL mode persists in the DB file after the first set. All
subsequent connections automatically use WAL without re-running the PRAGMA.

`_enable_sqlite_wal()` also calls `_register_sqlite_pragmas(engine)`, which registers a
SQLAlchemy `connect` listener running `PRAGMA synchronous=NORMAL` on every new connection.
Unlike `journal_mode`, `synchronous` is per-connection, so setting it once at startup would
only affect the first connection. NORMAL is the correct setting under WAL: it never risks
corruption, only the most recent transactions on an OS crash or power loss. Under the
default FULL, every commit pays an fsync, and with several agents plus the dashboard and
scheduler writing to one file, commits queue long enough that other writers exhaust the
30 s busy timeout and fail with "database is locked".

All SQLite engines are also created with `connect_args={"check_same_thread": False, "timeout": 30}`.
The `timeout` maps to `sqlite3.connect(timeout=30)` (Python's busy-wait duration in seconds).
This prevents instant `SQLITE_BUSY` failures when the old and new uvicorn workers briefly
overlap during a `--reload` restart. `PRAGMA busy_timeout` is redundant with `connect_args`
timeout: only the latter is needed since it applies to all pooled connections.

### Extracting a filesystem path from a SQLite URL

`_get_db_path_from_url()` (`sova/db/session.py`) is the sole place that turns a SQLite
connection string into a `Path`, and every caller (`init_db()`, `init_db_for_project()`,
`_backup_db()`) relies on it returning `None` for anything that is not a real file on disk.
Two forms are not real files and must resolve to `None`: an in-memory URL
(`sqlite+aiosqlite:///:memory:`, or a query-string form like `?mode=memory`), and a query
string appended to an otherwise-real path (`sqlite+aiosqlite:///path/to.db?uri=true`), which
must have the query stripped before `Path()` construction rather than be included verbatim.
Treating either as a real file risks creating a stray `:memory:` file on disk, or chmod'ing
and backing up the wrong (query-suffixed) path. This has recurred across two independent
PRs (#1239, #1240) hitting the same gap from different angles; when adding a new caller of
this helper, do not re-derive the path from the URL by hand.

### Cancellation and the write lock

SQLite holds the write lock of an abandoned connection until the owning process exits. A
task cancelled between `INSERT` and `COMMIT` drops its connection mid-transaction, and the
whole project database then becomes unwritable: no run can be finalized, the liveness
sweep cannot reclaim dead runs, and no new agent can be registered. Recovery requires
restarting the server.

Any DB write that can run on a cancellable path must therefore be shielded:

```python
await asyncio.shield(self._insert(records))
```

This applies to every writer on the agent-exit path, which `cancel_background_tasks()`
cancels with a 3 s timeout: `OutputWriter.flush`, `ResourceWriter.flush`, and
`ResourceWriter.write_summary`. When re-queuing buffered rows after a cancel, distinguish
the two cases with `asyncio.current_task().cancelling()`: if our own task was cancelled the
shielded write still completes, so re-queuing would duplicate rows; if the write itself was
cancelled before landing, the rows must be kept.

### Engine disposal after migrations

`_run_migrations()` returns `bool`: `True` if DDL was executed (Alembic ran at least one
migration), `False` if the DB was already at the head revision (fast path). For a file-backed
SQLite engine, `init_db()`/`init_db_for_project()` now dispose the engine once `_run_migrations()`
returns, regardless of that return value, before calling `_enable_sqlite_wal()`: see "SQLite
foreign keys" below for why this can no longer be conditioned on DDL having run. `init_db()` also
disposes whatever engine the module-level `_engine` previously pointed at before replacing it,
but only when that previous engine is no longer reachable through `_engines` (i.e. a second call
for the *same* project/url, which overwrites that dict entry in place) **and** the url is
file-backed (`_get_db_path_from_url(url) is not None`). An in-memory SQLite engine's connection
pool *is* the database (see `tests/conftest.py`'s `close_db()` warning), not just a reclaimable
cache of connections to one, so disposing it destroys data rather than freeing a pool; a
file-backed engine's pool is safe to reclaim this way because the data lives in the file, not the
pool. A second call for a *different* project/url leaves the previous engine's `_engines` entry
untouched, so it is not disposed either way: disposing it anyway would pull the pool out from
under any other caller still holding that factory via `get_session(project_dir=...)` or
`get_session_factory()`. Prefer `init_db_for_project()` for anything scoped to one project; it
only ever adds a new entry to `_engines`, never replaces the process-global
`_engine`/`_session_factory` another caller's bare `get_session()` depends on
(`sova/dashboard/routers/setup.py`'s `/api/setup/configure` route is the example this fixed: it
used to call `init_db()` and silently redirect every other request's default-path
`get_session()` onto the just-configured project for the rest of the process). `init_db()`'s
whole body (engine creation, migrations, WAL/pragma setup) runs under the same per-url
`_get_init_lock()` guard `init_db_for_project()` uses, so two concurrent callers resolving to the
same url (e.g. two requests both hitting `get_session()` right after startup) are serialized
rather than each creating an engine and racing to dispose the other's mid-migration.

### Migration conventions

1. **Use `batch_alter_table`** for all SQLite DDL (env.py has `render_as_batch=True`)
2. **Idempotent checks**: use `_column_exists()`, `_table_exists()`, `_index_exists()` helpers (defined in migrations 006, 008). A `drop_constraint()`/`create_foreign_key()` pair needs the same care: `Base.metadata` has no `naming_convention`, so a table built by `create_all()` (the pre-#1205 self-healing fallback's own path) gives a FK no name at all, not Alembic's generated one. Reflect the actual constraint first (`sa.inspect(op.get_bind()).get_foreign_keys(table)`), skip if it already matches the desired `ondelete`, and when dropping an unnamed one pass `naming_convention=` to `batch_alter_table()` so Alembic can assign it a deterministic name to drop by, rather than assuming the constraint carries whatever name a fresh migration-built chain would give it (migration 040 fixed exactly this)
3. **Sequential numbering**: `001` onward (not Alembic UUIDs)
4. **Pre-migration backup**: `_backup_db()` writes a WAL-consistent copy (`sqlite3.Connection.backup()`, not a byte-level file copy) immediately before DDL executes, inside `_run_migrations()` itself, not after. Only runs when DDL is actually about to run, and only for a non-empty database file. Rotates any existing `.db.bak` to `.db.bak.1` first, so at least two generations survive a crash mid-write
5. **Narrowed fallback**: Alembic failing on a database that already has tables (pre-Alembic with real data, or behind head) re-raises instead of being swallowed, so `init_db()` fails loudly. `create_all` + stamp only runs for a genuinely fresh database (no tables, no `alembic_version`) in the first place
6. **Cross-process serialization**: `init_db()`/`init_db_for_project()` wrap both `_run_migrations()` and `_enable_sqlite_wal()` (WAL mode, pragmas, orphan cleanup, now run strictly *after* migrations, see "SQLite foreign keys" below) in a `flock()`-based lock file (`<db>.migrate.lock`, i.e. `sova.db.migrate.lock` beside the database: appended to the filename, not substituted for its suffix, so it cannot collide with a sibling SQLite file sharing the same stem), since the CLI, the dashboard/scheduler server, and every `sova run` subprocess are separate processes that can otherwise race the same `.claude/sova.db` with nothing stronger than SQLite's busy timeout between them. Released automatically if the holding process crashes. A hung (not crashed) holder is bounded too: acquisition polls with a non-blocking `flock()`, logs a warning naming the lock path after 5s of contention, and gives up with a clear error after 120s rather than hanging forever. The in-process guard (`_get_init_lock()`, an `asyncio.Lock`) is keyed per database url, not global, so `init_db_for_project()` calls for two different projects never queue behind each other even while one holds the cross-process flock for tens of seconds

### The five alembic_version cases

Handled in `_run_migrations()` (`sova/db/session.py`):

1. **Fresh DB** (no tables): run all migrations from scratch; if this fails, fall back to `create_all` + stamp (the only case where that fallback is safe)
2. **Pre-Alembic DB** (tables exist, no `alembic_version`): stamp at current head; a failure here re-raises rather than falling back, since real data already exists
3. **Already at head** (version == `_get_alembic_head()`): verify the schema via `_find_missing_columns()`, logging an error naming any ORM column the database lacks, then return `False`; skip upgrade, dispose, and backup. The check compares names only, so it stays cheap enough for the startup path. It reports rather than repairs, because a column with no migration behind it cannot be recreated from the revision history
4. **Behind head** (version < head): back up, then upgrade to head; dispose pool. A failure during upgrade re-raises instead of falling back to `create_all` + stamp, since that would silently mask a partially-applied migration as fully applied
5. **Empty/corrupted `alembic_version`**: a missing row, or a row whose `version_num` is `NULL`/empty/whitespace-only, is dropped (`DROP TABLE alembic_version`) and treated as case 1; a version string that fails to resolve to a real revision (rather than a blank one) is case 2 or 4 depending on whether other tables exist, and re-raises on failure like they do. `has_tables` (used both to pick stamp-vs-upgrade and to decide whether a later failure may fall back to `create_all`) always excludes `alembic_version` itself from the table count, so a database whose only table is a corrupted `alembic_version` is correctly treated as "no tables at all", not as "real data present"

### SQLite foreign keys

`_register_sqlite_pragmas()`'s connect listener sets `PRAGMA foreign_keys=ON`, so
`ondelete="CASCADE"`/`ondelete="SET NULL"` columns (e.g. `MemoryEdge.source_id`/`target_id`,
`TaskRun.workflow_definition_id`) actually cascade/null out on SQLite the way they always have on
Postgres. `_enable_sqlite_wal()` (which calls `_register_sqlite_pragmas()`) must run only *after*
`_run_migrations()` has finished on the same engine, never before: Alembic's SQLite batch mode
(required for any `ALTER`/`DROP COLUMN`, used 8+ times already on `task_runs` alone) does
`CREATE tmp` / `INSERT` / `DROP TABLE <orig>` / `RENAME`, and with `foreign_keys=ON` that `DROP`
of a table other populated tables reference raises `IntegrityError`. `init_db()`/
`init_db_for_project()` therefore call `_run_migrations()` first, then unconditionally dispose the
engine (evicting any connection migrations opened before the pragma listener existed, so it is
never reused pragma-less for the rest of the engine's lifetime), then call `_enable_sqlite_wal()`.
That dispose is why engine disposal (see above) can no longer be conditioned on whether DDL
actually ran. `_enable_sqlite_wal()` then runs a `_cleanup_orphaned_memory_edges()` sweep, since a
database created before the pragma was enabled could already hold `memory_edges` rows whose
`memories` row is gone. The sweep is genuinely one-time rather than a per-startup table scan: once
it has run (orphans found and removed, or none found), it writes a `project_settings` marker row
(`db.orphan_edges_swept`), and every later call returns immediately on seeing that row, since
`ondelete="CASCADE"` with the pragma now active means no new orphan can ever accumulate again. It
takes its own `_backup_db()` snapshot, under its own suffix (`.db.bak.pre-orphan-cleanup`, never
the pre-migration `.db.bak`/`.db.bak.1` slots `_run_migrations()` uses), immediately before the
`DELETE` when an orphan is actually found, since that delete is otherwise the one destructive
write this module performs outside of migrations with no restore point of its own; using a
separate suffix means this sweep's backup can never demote (or be demoted by) the pre-migration
restore point `_run_migrations()` took moments earlier in the same startup.

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

# Database Guidelines

ORM conventions, session management, and migration patterns for SOVA's async SQLAlchemy stack.

## Stack

- **ORM**: SQLAlchemy 2.0 async (`DeclarativeBase`, `Mapped`, `mapped_column`)
- **Async driver**: `aiosqlite` (default), `asyncpg` (PostgreSQL)
- **Sync driver (config bridge)**: `sqlite3` (default), `psycopg` (PostgreSQL); `pip install 'sova[postgres]'` installs both PostgreSQL drivers
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

24 models in `sova/db/models.py`: `TaskRun`, `OutputLine`, `StepExecution`, `FailureRecord`, `CostRecord`, `TaskAssessmentRecord`, `IssueLifecycle`, `LifecyclePhaseRecord`, `WorkflowDefinition`, `CommandContract`, `CodeRabbitEvent`, `ResourceSampleRecord`, `ResourceSummaryRecord`, `ActionFeedback`, `OversightRun`, `OversightFinding`, `PRCreationQueue`, `TelemetryEvent`, `SupervisorDecision`, `PREvent`, `ProjectSetting`, `MergeQueueEntry`, `FeedEventRecord`, `BudgetOverride`.

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

# DB query terminals (4 states, including "interrupted")
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
cleanup is deliberately not planned (issue #1008), and the guard is scoped to stay green
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
2. **Idempotent checks**: use `_column_exists()`, `_table_exists()`, `_index_exists()` helpers (defined in migrations 006, 008). A migration that adds a column to an existing table should also skip a table that does not exist yet (041's `_column_state()` returns None for it): a partial schema is completed by the create_all fallback, which builds the table with the column already present. A `drop_constraint()`/`create_foreign_key()` pair needs the same care: `Base.metadata` has no `naming_convention`, so a table built by `create_all()` (the pre-#1205 self-healing fallback's own path) gives a FK no name at all, not Alembic's generated one. Reflect the actual constraint first (`sa.inspect(op.get_bind()).get_foreign_keys(table)`), skip if it already matches the desired `ondelete`, and when dropping an unnamed one pass `naming_convention=` to `batch_alter_table()` so Alembic can assign it a deterministic name to drop by, rather than assuming the constraint carries whatever name a fresh migration-built chain would give it (migration 040 fixed exactly this)
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

### `sova db status` / `sova db backup`

`sova db status` prints the current revision, the head revision, whether they match, and any
`_find_missing_columns()` drift; a database with no `alembic_version` table at all prints a
plain "not initialized" message instead of raising. Both read the database through
`inspect_schema()` (`sova/db/session.py`), which returns a `SchemaInspection` (`state` of
`no_file`, `no_tables` or `ready`, plus the revision, head and drift fields only meaningful when
`ready`) and never creates, migrates or stamps anything. `sova doctor` reuses it for its
`database schema` row, a warning (never a required failure, since `init_db()` migrates a database
that is behind on the next server start) naming an unversioned database, a revision behind head,
or missing ORM columns; a project with no database file gets no row, because the `database` check
already reports that. `sova db backup` calls the same
`_backup_db()` used internally before DDL, writing a WAL-consistent copy and printing the path
(or exiting non-zero when there is nothing to back up: no file yet, or an empty one). Against a
non-SQLite deployment (`SOVA_DATABASE_URL` pointing at Postgres) it instead prints an explicit
"backup is only supported for SQLite" message rather than the misleading "nothing to back up"
one, since the database is neither missing nor empty, backup for that backend is simply
unsupported. `_run_migrations()` logs the backup path it wrote (`_log.info`) whenever one is
taken, so the restore target is visible in the logs next to any migration failure that follows.
Both commands resolve their url via `_get_database_url(resolved_dir, create_dirs=False)`: the
default (`create_dirs=True`) path every other caller uses creates `.claude/` as a side effect of
just computing the path, which would leave a stray `.claude/` directory behind on a directory
that is not a SOVA project at all, just because a read-only diagnostic command was pointed at it.
`status` additionally checks the file exists before creating an engine against it, so it never
even opens a connection to a path with no database file.

### SQLite foreign keys

`_register_sqlite_pragmas()`'s connect listener sets `PRAGMA foreign_keys=ON`, so
`ondelete="CASCADE"`/`ondelete="SET NULL"` columns (e.g. `ResourceSampleRecord.task_run_id` for CASCADE and
`TaskRun.workflow_definition_id` for SET NULL) actually cascade/null out on SQLite the way they always have on
Postgres. `_enable_sqlite_wal()` (which calls `_register_sqlite_pragmas()`) must run only *after*
`_run_migrations()` has finished on the same engine, never before: Alembic's SQLite batch mode
(required for any `ALTER`/`DROP COLUMN`, used 8+ times already on `task_runs` alone) does
`CREATE tmp` / `INSERT` / `DROP TABLE <orig>` / `RENAME`, and with `foreign_keys=ON` that `DROP`
of a table other populated tables reference raises `IntegrityError`. `init_db()`/
`init_db_for_project()` therefore call `_run_migrations()` first, then unconditionally dispose the
engine (evicting any connection migrations opened before the pragma listener existed, so it is
never reused pragma-less for the rest of the engine's lifetime), then call `_enable_sqlite_wal()`.
That dispose is why engine disposal (see above) can no longer be conditioned on whether DDL
actually ran.

### Dropping the memory tables (migration 044)

`044_drop_memories_tables` removed the `memories` and `memory_edges` tables (issue #1464). Before dropping
anything it exports every row of both tables to one JSON file, `<project>/.claude/memories-export-<UTC timestamp,
microseconds>.json`, with the keys `memories` and `memory_edges`. `_run_migrations(engine, project_dir)` hands the
migration the project's `.claude/` through the Alembic config (`MEMORIES_EXPORT_DIR_ATTRIBUTE` in
`sova/db/session.py`, shared with the migration by name); `init_db()` called with no `project_dir` uses the ambient
project context (`get_project_dir()`, `None` when no context is set) only when the database is that project's own
`.claude/sova.db`, so the global database (`~/.config/sova/sova.db`) never exports into a project's working tree. A
run with no project (the bare `alembic` CLI, or the global database) uses the SQLite file's own directory, and any
other database falls back to the stable `~/.config/sova`, never the current directory. The absolute path, the row counts and where the location came from
(`project`, `database_file` or `fallback_state_dir`) are logged at WARNING as `db.memories_exported`. Each
machine or installation runs the migration on its own database and writes its own file. On a database shared by
several projects (unsupported, see "PostgreSQL: one database per project, never shared" below), only the project
whose process runs the migration first receives the export, and it holds every project's rows, told apart by the
`repo` column. Details:

- No file is written when both tables are empty or absent, and the tables are dropped in either case.
- A file is created exclusively (never overwritten; a name clash takes a numeric suffix) with owner-only mode
  `0o600` (it holds free-text agent output) and flushed
  before the drop. If the write fails, the migration raises with both tables intact and the database stays at revision 043.
- Keeping it out of version control: SOVA's own `.gitignore` covers `.claude/memories-export-*.json`, and for every
  other project the name prefix `.claude/memories-export-` is in `RUNTIME_STATE_PATHS`
  (`sova/git/artifact_exclusions.py`; `pathspec_include()` and `INFO_EXCLUDE_ENTRIES` expand a prefix without a
  trailing `/` to `.claude/memories-export-*`, which `tests/test_untracked_collisions.py` checks), so `git add -A` from the commit step never stages it and `sova install` writes
  it into `.git/info/exclude` (re-run `sova install` to add it to an existing project's exclude file). A human
  running `git add .claude` in a project without that entry can still commit it: move the file out of the repository.
- Rows are read as stored, with no column type coercion, so a value the ORM types cannot parse (a malformed
  datetime string) still exports. On SQLite a datetime is the stored text; on PostgreSQL datetimes are ISO strings,
  `Decimal` a string and bytes base64. `memories.embedding` (a JSON list) is decoded back to a list.
- The migration also deletes the `db.orphan_edges_swept` row the removed edge cleanup left in `project_settings`.
- Nothing re-imports the file; the downgrade recreates both tables empty. On SQLite, the pre-migration `.db.bak`
  backup also still holds the rows; on PostgreSQL the export file is the only copy.

## Multi-Project DB Support

`init_db_for_project()` caches per-project engines in `_engines: dict[str, tuple]` keyed by URL. `get_session(project_dir=...)` routes to the correct engine. Each project gets its own `.claude/sova.db` file.

### PostgreSQL: one database per project, never shared

`SOVA_DATABASE_URL` points the whole process at a single database, and the tables are not consistently scoped to a project. Some carry an indexed `project_slug` that is written and filtered (`TaskAssessmentRecord`, `OversightFinding`, `TelemetryEvent`, `PRCreationQueue`); `TaskRun` declares the column and its index but nothing writes it, so it stays at its `""` default and every `select(TaskRun)` runs unscoped; `CostRecord` has no such column at all. So two projects pointed at the same Postgres database would silently mix each other's runs, costs, and memory rows, and the liveness sweep would mark another machine's runs interrupted. Give each project its own database (or at minimum its own schema with a dedicated role), never a shared one. The `deploy/sova-server.service` and `deploy/com.sova.server.plist` reference templates deliberately omit a `SOVA_DATABASE_URL` example for this reason: a copy-pasted shared connection string across multiple deployed instances is exactly the failure mode this note exists to prevent. Populating and filtering `project_slug` across every query is tracked separately and out of scope for a single-project deployment.

## Config Cache

`load_config()` is synchronous and many `async def` request paths call it, so it must not build an engine or parse TOML on every call.

- **One database for reads and writes**: `_try_load_from_db()` derives its sync URL from `_get_database_url()` (the resolution `get_session()` uses) through `db_loader._async_to_sync_url()`: `sqlite+aiosqlite` to `sqlite`, `postgresql` and `postgresql+asyncpg` to `postgresql+psycopg`. A `SOVA_DATABASE_URL` therefore reaches both paths. Do not reimplement this mapping in the CLI or in `settings_service.py`.
- **Unreadable database fails closed**: an unmapped dialect, or PostgreSQL without `psycopg`, logs one error per process (dialect or package name only, never the URL) and falls back to the non-DB layers; it never falls back to the local SQLite file. Every config writer refuses the write via `config_db_unreadable_reason()`, because the value would not be read back (including the fleet manager's per-project `max_parallel_agents` write, which returns False): `sova config set`, `sova migrate-config`, `sova install` and `sova setup` (exit 1), `POST /api/settings/config` and `POST /api/setup/configure` (HTTP 422). `sova migrate-config` requires the database file to exist only for SQLite (the file `_sync_url_for()` names, not a hardcoded `.claude/sova.db`).
- **Cache**: process-wide, per resolved `project_dir`, in `sova/config/loader.py`. Each call returns a deep copy. A hit is valid while `db_loader.config_fingerprint()` is unchanged: the `sova.toml` stamp, a hash of the `SOVA_*` environment (names matched case-insensitively, as the build reads them), and for SQLite the database file's inode (a replaced file) plus a **settings stamp file** (`<db>.settings-stamp`, e.g. `.claude/sova.db.settings-stamp`). The database file and its `-wal` sidecar are deliberately not stamped: in WAL mode run output, TaskRun, StepExecution and cost writes change them several times a second, which would turn nearly every call on a busy server into a rebuild. `invalidate_config_cache(project_dir)` touches the stamp (`touch_settings_stamp`, an atomic replace so the inode changes even on a coarse-timestamp filesystem), and `_save_config_to_db_sync` touches it after a successful write, so a settings write from another process (the CLI against a running server) is caught. Every settings writer must therefore call `invalidate_config_cache(project_dir)` after its commit, in a separate process too. A writer that does not (the `sqlite3` shell) is picked up when the SQLite entry expires after `SQLITE_CONFIG_CACHE_TTL_S` (60 seconds).
- **PostgreSQL staleness**: there is no file to stamp, so an entry expires after `PG_CONFIG_CACHE_TTL_S` (5 seconds). A write from another process shows up within that window.
- **Invalidate after a committed write**: call `invalidate_config_cache(project_dir)` (no argument clears every project). Generation counters (one per project, plus a global one for the no-argument form) stop a rebuild that started before the invalidation from storing its stale result, without discarding a concurrent rebuild of another project.
- **asyncpg URL query is rewritten for the sync driver**: `_async_to_sync_url()` maps `postgresql+asyncpg` to `postgresql+psycopg`, which hands the query string to libpq. `ssl=<mode>` becomes `sslmode` (`true` is `require`, `false` is `disable`; an existing `sslmode` wins), and asyncpg-only keys (`prepared_statement_cache_size`, `prepared_statement_name_func`, `statement_cache_size`, `max_cached_statement_lifetime`, `max_cacheable_statement_size`, `command_timeout`) are dropped. An `ssl` value with no libpq equivalent (for example a path) raises `UnsupportedDatabaseError` naming the parameter, so the write guard fires.
- **Locked or unreachable database**: on a cache miss the last good entry is served with a warning. With no earlier entry the non-DB layers are served with a warning. Either result is cached as a degraded entry, whatever the fingerprint says, so an outage costs one failed read and one warning per window instead of one per call. The window starts at `DB_FAILURE_BACKOFF_S` (15 seconds, well above the 2 second `PG_CONNECT_TIMEOUT_S`, so a blackholed host blocks the calling thread for a small fraction of the outage), doubles with each consecutive failure up to `DB_FAILURE_BACKOFF_MAX_S` (120 seconds), and resets on the first successful read; an in-process invalidation clears it. An explicit `?connect_timeout=` in `SOVA_DATABASE_URL` is kept, never replaced by the default. The last good entry is served only while the `SOVA_*` environment and `sova.toml` stamp are unchanged, and only if it actually holds DB settings (a degraded entry built from the non-DB layers is not a last good config, so a repeat failure logs `loader.db_unreachable_settings_not_applied`). A degraded entry also ends as soon as the environment or `sova.toml` changes, even inside the window, so a TOML edit or env override (including a corrected `SOVA_DATABASE_URL`) is never frozen out.
- **Sync bridge paths**: the sync read (`_try_load_from_db`), the emptiness probe (`_is_db_confirmed_empty`) and the plain save build their engine from `_sync_url_for()` and `_engine_kwargs()`, so they follow `SOVA_DATABASE_URL`. The TOML auto-migration (`_save_config_to_db_sync(only_if_empty=True)`) relies on `BEGIN IMMEDIATE` and is SQLite-only: for another dialect it logs one error per process and does not run, and `_is_db_confirmed_empty` returns False without opening a connection, so a PostgreSQL cache miss costs one sync read.
- **Tests**: `tests/conftest.py` clears the cache around every test. `tests/test_config_cache.py` also holds an AST guard: no `async def` may call the synchronous `create_engine`; read config through `load_config()` or build the engine in a sync helper.

## Test Fixtures

```python
pytestmark = pytest.mark.usefixtures("reset_db_rows")
```

Tests share one in-memory SQLite engine per pytest-xdist worker, emptied before each test by `reset_db_rows` (`tests/conftest.py`). Never build a per-test engine or write `SOVA_DATABASE_URL` through `os.environ` (full entry: docs/testing-guidelines.md, "In-Memory Database Fixture").

## Index Naming

All indexes follow `ix_{table}_{column}` pattern: `ix_task_runs_issue`, `ix_memories_category`, `ix_assessments_project_slug`. Single-column indexes cover current query patterns.

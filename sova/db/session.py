"""Async database session factory for SOVA.

Supports multi-project mode with per-project DB sessions.
Uses Alembic migrations for schema changes (not create_all).
Backs up SQLite databases before migrations apply DDL (not on every restart).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.db.models import Base

_engine = None
_session_factory = None
_engines: dict[str, tuple] = {}

_DB_FILENAME = "sova.db"
_log = logging.getLogger("sova.db")
_init_locks: dict[str, asyncio.Lock] = {}

_LOCK_POLL_INTERVAL_S = 0.5
_LOCK_WARN_AFTER_S = 5.0
_LOCK_TIMEOUT_S = 120.0


def _get_init_lock(url: str) -> asyncio.Lock:
    """Lazily create a per-URL init lock (must be called inside a running event loop).

    Per-URL rather than global: a project A migration holding the cross-process
    flock for tens of seconds must not stall every concurrent
    ``init_db_for_project()`` call for an unrelated project B behind this
    asyncio-level guard too.
    """
    lock = _init_locks.get(url)
    if lock is None:
        lock = asyncio.Lock()
        _init_locks[url] = lock
    return lock


def _get_database_url(project_dir: Path | None = None, *, create_dirs: bool = True) -> str:
    """Resolve the database URL.

    Priority:
    1. SOVA_DATABASE_URL env var (for PostgreSQL team deployments)
    2. SQLite file in project's .claude directory
    3. SQLite file in default location

    ``create_dirs=False`` skips the mkdir side effect, for a read-only caller
    (``sova db status``/``backup``) that must not create ``.claude/`` on a
    directory merely because the user pointed a diagnostic command at it.
    """
    env_url = os.environ.get("SOVA_DATABASE_URL")
    if env_url:
        if env_url.startswith("postgresql://"):
            return env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return env_url

    if project_dir:
        if not isinstance(project_dir, Path):
            raise TypeError(f"project_dir must be a Path, got {type(project_dir).__name__}")
        db_path = project_dir / ".claude" / _DB_FILENAME
    else:
        db_path = Path.home() / ".config" / "sova" / _DB_FILENAME

    if create_dirs:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def _get_db_path_from_url(url: str) -> Path | None:
    """Extract the file path from a SQLite URL, or None for non-SQLite/in-memory."""
    if not url.startswith("sqlite"):
        return None
    path_str = url.split("///", 1)[-1] if "///" in url else None
    if not path_str:
        return None
    path_str = path_str.split("?", 1)[0]
    if path_str == ":memory:" or "mode=memory" in url:
        return None
    return Path(path_str)


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _secure_db_file(db_path: Path) -> None:
    """Restrict a SQLite database file, and any active WAL/SHM sidecar, to
    owner read/write only.

    The file holds secrets (provider API keys, Jira/webhook tokens, etc. in
    `project_settings`), so group/world access must be denied regardless of
    the process umask. Called once per file creation (startup, and once per
    backup copy), mirroring the existing one-time `mkdir(parents=True,
    exist_ok=True)` pattern in `_get_database_url`, never on every
    `get_session()` call.

    A WAL-mode database under an open connection keeps committed rows in a
    `-wal` sidecar (and a `-shm` index) rather than the main file, created
    with the main file's permissions at the time SQLite first opened it; a
    main file secured after the fact leaves a stale, more permissive sidecar
    behind, so both are chmod'd here alongside the main path. A chmod failure
    is propagated rather than logged-and-ignored: silently continuing past it
    would leave a secrets-bearing file at its prior, possibly world-readable
    permissions while callers proceed as though it were secured.
    """
    paths = (db_path, *(db_path.with_name(db_path.name + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES))
    for path in paths:
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
        except OSError:
            _log.exception("db.chmod_failed path=%s", path)
            raise


def _ensure_db_file_secured_before_open(db_path: Path) -> None:
    """Secure a SQLite database file before any engine connects to it.

    SQLite creates a new file honoring the process umask (typically world- or
    group-readable), and the file sits at that permissive mode for however
    long migrations take to run before the post-migration `_secure_db_file()`
    call. For an existing file, chmod it now rather than after connecting.
    For a missing one, pre-create it at 0600 so SQLite's own open() reuses
    that file instead of creating a new, more permissive one, closing the
    exposure window instead of merely narrowing it. A concurrent creator
    racing this process is handled the same as an existing file.
    """
    if db_path.exists():
        _secure_db_file(db_path)
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(db_path, os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        _secure_db_file(db_path)


def _backup_db_sync(db_path: Path, *, backup_suffix: str = ".db.bak") -> Path:
    """Write a WAL-consistent backup, keeping one prior generation.

    Uses ``sqlite3.Connection.backup()`` (the SQLite Online Backup API) rather
    than copying file bytes: a plain copy of the main file can miss pages still
    sitting in the ``-wal`` file, producing a backup that silently predates the
    last committed transactions. Rotates any existing backup at
    ``<backup_suffix>`` to ``<backup_suffix>.1`` (overwriting a prior ``.1``)
    first, so at least two generations survive a crash during the write.
    ``backup_suffix`` defaults to the pre-migration restore point
    (``.db.bak``); callers with a different restore point to protect (e.g. the
    orphan-edge cleanup sweep) pass a distinct suffix so they rotate their own
    slots instead of demoting the pre-migration backup.
    """
    backup = db_path.with_suffix(backup_suffix)
    previous = db_path.with_suffix(f"{backup_suffix}.1")
    if backup.exists():
        backup.replace(previous)

    # Pre-create the destination at 0600 before SQLite opens it, so the backup's
    # secret-bearing content is never written through a umask-derived, possibly
    # world-readable mode. The rotation above moved any prior backup aside, so
    # O_EXCL normally succeeds; a concurrent creator racing it is handled by
    # chmod'ing the existing file instead.
    try:
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        os.chmod(backup, 0o600)

    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        dest = sqlite3.connect(str(backup), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return backup


async def _backup_db(url: str, *, backup_suffix: str = ".db.bak") -> Path | None:
    """Back up a SQLite database before migrations (or other destructive writes) apply."""
    db_path = _get_db_path_from_url(url)
    if db_path is None or not db_path.exists() or db_path.stat().st_size == 0:
        return None
    # _backup_db_sync writes a WAL-consistent copy via SQLite's Online Backup
    # API (a plain file copy can miss pages still in the -wal sidecar). The
    # backup holds the same secrets as the live DB, so secure it afterwards:
    # sqlite3 creates the destination honoring the process umask.
    backup = await asyncio.to_thread(_backup_db_sync, db_path, backup_suffix=backup_suffix)
    _secure_db_file(backup)
    return backup


_ALEMBIC_HEAD_CACHE: str | None = None


def _get_alembic_head(alembic_cfg) -> str | None:
    """Return the current head revision from migration scripts, or None on error.

    Result is cached for the process lifetime: the head revision never changes
    while the server is running, so repeated restarts only pay the I/O cost once.
    """
    global _ALEMBIC_HEAD_CACHE
    if _ALEMBIC_HEAD_CACHE is not None:
        return _ALEMBIC_HEAD_CACHE
    try:
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(alembic_cfg)
        result = script.get_current_head()
        if result is not None:
            _ALEMBIC_HEAD_CACHE = result
        return result
    except Exception:  # noqa: BLE001 (alembic surfaces arbitrary errors; head detection is advisory)
        _log.debug("alembic.head_cache_failed", exc_info=True)
        return None


def _find_missing_columns(sync_conn) -> list[str]:
    """Return "table.column" entries the ORM declares but the database lacks.

    Alembic tracks only which revisions ran, so a column added to a model without
    a migration leaves the database permanently stamped at head yet missing that
    column. Nothing in the revision number can reveal that; only the live schema
    can. Compares names only (not types or nullability) to stay cheap enough for
    the already-at-head startup path.
    """
    inspector = inspect(sync_conn)
    live_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in live_tables:
            missing.append(f"{table_name} (entire table)")
            continue
        live_columns = {c["name"] for c in inspector.get_columns(table_name)}
        missing.extend(f"{table_name}.{c.name}" for c in table.columns if c.name not in live_columns)
    return sorted(missing)


@dataclass(frozen=True)
class SchemaInspection:
    """Read-only snapshot of one project database's migration state.

    ``state`` separates "nothing to inspect" from "inspected": ``no_file`` is a
    project whose database file does not exist yet, ``no_tables`` is a file with
    no tables in it. Only ``ready`` carries meaningful ``current``/``head``/
    ``missing`` values, so a caller never reads the defaults of the other two as
    "at head with no drift".
    """

    state: Literal["no_file", "no_tables", "ready"]
    legacy: bool = False
    current: str | None = None
    head: str | None = None
    missing: tuple[str, ...] = ()

    @property
    def at_head(self) -> bool:
        return self.current is not None and self.head is not None and self.current == self.head


async def inspect_schema(project_dir: Path) -> SchemaInspection:
    """Report where a project's database sits relative to the migration head.

    Strictly read-only: it never creates the project's ``.claude/`` directory,
    runs a migration, or stamps a revision, so ``sova db status`` and ``sova
    doctor`` can both call it against any path. A database that cannot be read
    raises; each caller decides whether that is fatal for itself.
    """
    from alembic.config import Config

    url = _get_database_url(project_dir, create_dirs=False)
    db_path = _get_db_path_from_url(url)
    if db_path is not None and not db_path.exists():
        return SchemaInspection(state="no_file")

    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if not table_names:
            return SchemaInspection(state="no_tables")

        legacy = "alembic_version" not in table_names
        current: str | None = None
        if not legacy:
            async with engine.connect() as conn:
                row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).fetchone()
            current = row[0] if row else None

        head = _get_alembic_head(Config(str(Path(__file__).parent / "alembic.ini")))

        async with engine.connect() as conn:
            missing = await conn.run_sync(_find_missing_columns)
    finally:
        await engine.dispose()

    return SchemaInspection(state="ready", legacy=legacy, current=current, head=head, missing=tuple(missing))


async def _run_migrations(engine) -> bool:
    """Run Alembic migrations programmatically.

    Handles four cases:
    1. Fresh DB (no tables): run all migrations from scratch
    2. Existing DB without alembic_version: stamp at current head (pre-Alembic DB)
    3. Existing DB with alembic_version at head: verify no ORM column is missing,
       then skip the upgrade (fast path)
    4. Existing DB with alembic_version behind head: upgrade to head

    Falls back to create_all + stamp only for case 1 (a database with no tables
    at all): that is the one case where stamping over whatever create_all
    produces cannot silently mask missing DDL on real data. Any failure on a
    database that already has tables (cases 2 and 4) propagates instead of
    being swallowed, so init_db() fails loudly rather than leaving
    alembic_version stamped past a migration that never actually ran. A SQLite
    backup is taken (consistent under WAL) immediately before DDL executes.

    Returns True if DDL was executed, False if the DB was already at head.
    Callers that go on to call _enable_sqlite_wal() must dispose the pool
    first regardless of this return value: see that function's docstring.
    """
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(Path(__file__).parent / "alembic.ini"))

    async with engine.connect() as conn:
        table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        has_alembic = "alembic_version" in table_names

    current_version: str | None = None
    if has_alembic:
        async with engine.begin() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
            version_value = (row[0] or "").strip() if row is not None else ""
            if not version_value:
                # No row, or a NULL/empty/whitespace version_num: tracking is corrupt
                # either way. Drop it and fall through to case 1 (fresh DB), matching
                # case 2/4 below which would otherwise hand Alembic's own
                # MigrationContext an unparseable revision and crash with a bare
                # AssertionError.
                await conn.run_sync(lambda c: c.execute(text("DROP TABLE alembic_version")))
                has_alembic = False
            else:
                current_version = version_value

    # Counts only real tables, never alembic_version itself: a database whose only
    # table is alembic_version (case 5, now dropped above) must be treated as "no
    # tables at all" so it falls into the self-healing path instead of propagating a
    # real-data failure at the `if has_tables: raise` below.
    has_tables = bool(set(table_names) - {"alembic_version"})

    # Fast path: DB is already at migration head, skip the upgrade entirely.
    # This saves ~300ms of Alembic script loading on every server restart.
    # Only safe when version is non-empty (empty string means corrupted tracking).
    if has_alembic and current_version:
        head = _get_alembic_head(alembic_cfg)
        if head is not None and current_version == head:
            # Being at head proves every revision ran, not that the schema matches the
            # ORM: a model column added without a migration is invisible to Alembic and
            # cannot be repaired by it either (upgrade is a no-op at head, and create_all
            # never alters an existing table). Verify and report so the cause is named
            # here rather than surfacing later as OperationalError on an unrelated query.
            # Purely diagnostic, so it fails open: a database this cannot inspect must
            # still start the server, exactly as it did before the check existed.
            log = logging.getLogger("sova.db")
            try:
                async with engine.connect() as conn:
                    missing = await conn.run_sync(_find_missing_columns)
            except Exception:  # noqa: BLE001 (diagnostic check must fail open; schema drift is reported, not fatal)
                log.warning("Could not verify schema against the ORM", exc_info=True)
                missing = []
            if missing:
                log.error(
                    "Schema drift: database is stamped at migration head %s but lacks %d ORM column(s): %s. "
                    "A model column was added without a migration; queries touching these tables will fail. "
                    "Add a migration that creates them.",
                    head,
                    len(missing),
                    ", ".join(missing),
                )
            return False  # Already at head, no DDL needed

    def _do_upgrade(sync_conn):
        alembic_cfg.attributes["connection"] = sync_conn
        command.upgrade(alembic_cfg, "head")

    def _do_stamp(sync_conn):
        alembic_cfg.attributes["connection"] = sync_conn
        command.stamp(alembic_cfg, "head")

    backup_path = await _backup_db(str(engine.url))
    if backup_path is not None:
        _log.info("Pre-migration backup written to %s", backup_path)

    try:
        if has_tables and not has_alembic:
            async with engine.begin() as conn:
                await conn.run_sync(_do_stamp)
        else:
            async with engine.begin() as conn:
                await conn.run_sync(_do_upgrade)
    except Exception:  # noqa: BLE001 (alembic surfaces arbitrary errors; re-raised below unless the DB was empty)
        if has_tables:
            # Real schema exists: stamping it at head here would hide a
            # partially-applied (or entirely unapplied) migration as if it had
            # succeeded. Let init_db() fail loudly instead.
            raise
        _log.warning("Alembic migration failed on a fresh database, falling back to create_all", exc_info=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: c.execute(text("DROP TABLE IF EXISTS alembic_version")))
                await conn.run_sync(_do_stamp)
        except Exception:  # noqa: BLE001 (tables exist either way; stamping is advisory)
            _log.warning("Alembic stamp also failed; tables created but untracked", exc_info=True)
    return True  # DDL was executed


def _register_sqlite_pragmas(engine) -> None:
    """Apply per-connection SQLite PRAGMAs to every new connection.

    ``journal_mode`` persists in the database file, but ``synchronous`` and
    ``foreign_keys`` are per-connection, so they have to be set on each connect
    rather than once at startup. NORMAL is the correct setting under WAL: it
    never risks corruption (only the most recent transactions on an OS crash or
    power loss) and avoids an fsync on every commit. With FULL, a machine
    running several agents spends long enough inside each commit that other
    writers exhaust the 30 s busy timeout and fail with "database is locked".
    SQLite ignores foreign key constraints unless ``PRAGMA foreign_keys=ON`` is
    set on the connection; without it, ``ondelete="CASCADE"`` columns such as
    ``MemoryEdge.source_id``/``target_id`` never actually cascade.
    """
    from typing import Any

    from sqlalchemy import event
    from sqlalchemy.pool import ConnectionPoolEntry

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: ConnectionPoolEntry) -> None:
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        except Exception:  # noqa: BLE001 (sqlite3 DB-API connection is opaque; pragma failure is advisory)
            _log.warning("Failed to set SQLite pragmas", exc_info=True)


_ORPHANED_MEMORY_EDGES_WHERE = (
    "source_id NOT IN (SELECT id FROM memories) OR target_id NOT IN (SELECT id FROM memories)"
)
_ORPHAN_SWEEP_MARKER_KEY = "db.orphan_edges_swept"
_ORPHAN_CLEANUP_BACKUP_SUFFIX = ".db.bak.pre-orphan-cleanup"


async def _orphan_sweep_already_done(conn) -> bool:
    row = await conn.execute(text("SELECT 1 FROM project_settings WHERE key = :key"), {"key": _ORPHAN_SWEEP_MARKER_KEY})
    return row.first() is not None


async def _mark_orphan_sweep_done(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO project_settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT(key) DO UPDATE SET value = :value"
            ),
            {"key": _ORPHAN_SWEEP_MARKER_KEY, "value": "true"},
        )


async def _cleanup_orphaned_memory_edges(engine) -> None:
    """Delete memory_edges rows whose source or target memory no longer exists.

    Databases created before PRAGMA foreign_keys was enabled could accumulate
    dangling edges, since ondelete="CASCADE" never fired on SQLite without it.
    Deleting the child-side (memory_edges) rows that already violate the
    constraint is never itself blocked by foreign key enforcement, so this is
    safe to run with the pragma already active. Once swept (orphans found and
    removed, or none found), a `project_settings` marker row is written so
    every later startup skips the scan entirely: with the pragma now active,
    ondelete="CASCADE" prevents new orphans from ever accumulating again, so
    the historical sweep is genuinely one-time rather than a per-startup table
    scan. A backup is taken immediately before the DELETE, under its own
    suffix (never the pre-migration `.db.bak` slot `_run_migrations()` uses,
    so this sweep's restore point can't demote or be demoted by that one).
    Purely a hygiene pass, so it fails open like _find_missing_columns: a
    database this cannot inspect must still start.
    """
    try:
        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
            if "memory_edges" not in table_names or "memories" not in table_names:
                return
            if "project_settings" in table_names and await _orphan_sweep_already_done(conn):
                return
            exists = await conn.execute(
                text(f"SELECT 1 FROM memory_edges WHERE {_ORPHANED_MEMORY_EDGES_WHERE} LIMIT 1")
            )
            found_orphan = exists.first() is not None

        if not found_orphan:
            if "project_settings" in table_names:
                await _mark_orphan_sweep_done(engine)
            return

        backup_path = await _backup_db(str(engine.url), backup_suffix=_ORPHAN_CLEANUP_BACKUP_SUFFIX)
        if backup_path is not None:
            _log.warning("Backup written to %s before removing orphaned edges", backup_path)

        async with engine.begin() as conn:
            result = await conn.execute(text(f"DELETE FROM memory_edges WHERE {_ORPHANED_MEMORY_EDGES_WHERE}"))
            if result.rowcount:
                _log.warning(
                    "Removed %d orphaned memory_edges row(s) referencing a deleted memory",
                    result.rowcount,
                )

        if "project_settings" in table_names:
            await _mark_orphan_sweep_done(engine)
    except Exception:  # noqa: BLE001 (diagnostic cleanup must fail open; startup must not depend on it)
        _log.warning("Failed to clean up orphaned memory_edges rows", exc_info=True)


async def _enable_sqlite_wal(engine) -> None:
    """Enable WAL journal mode and register per-connection pragmas.

    Must be called only after ``_run_migrations()`` has completed on this
    engine, never before: Alembic's SQLite batch mode (``move-and-copy``,
    required for any ``ALTER``/``DROP COLUMN``) does CREATE tmp / INSERT /
    DROP TABLE <orig> / RENAME, and ``PRAGMA foreign_keys=ON`` turns that DROP
    of a table other populated tables reference into an ``IntegrityError``.
    The caller is responsible for disposing the engine between
    ``_run_migrations()`` and this call, so that any connection opened during
    migration (before the pragma listener below existed) is evicted from the
    pool rather than reused pragma-less for the rest of the engine's
    lifetime. WAL mode persists in the database file after the first set.
    Subsequent connections automatically use WAL without re-running the
    PRAGMA. The busy timeout is handled per-connection via
    connect_args={"timeout": 30}.
    """
    _register_sqlite_pragmas(engine)
    await _cleanup_orphaned_memory_edges(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
    except Exception:  # noqa: BLE001 (WAL mode is an optimization; non-SQLite backends and locked DBs both fail here)
        _log.warning("Failed to set SQLite WAL mode", exc_info=True)


def _acquire_migration_lock_sync(lock_path: Path) -> IO[str]:
    """Open and flock() the migration lock file.

    Polls with a non-blocking LOCK_EX rather than blocking indefinitely, so a
    hung holder (as opposed to a crashed one, which flock already releases
    automatically) is diagnosable: a warning names the lock path after
    ``_LOCK_WARN_AFTER_S``, and acquisition gives up with a clear error after
    ``_LOCK_TIMEOUT_S`` instead of wedging the caller forever.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    waited = 0.0
    warned = False
    while True:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if waited >= _LOCK_TIMEOUT_S:
                fd.close()
                raise TimeoutError(
                    f"Timed out after {_LOCK_TIMEOUT_S:.0f}s waiting for migration lock at {lock_path}; "
                    "another process may be hung while holding it"
                ) from None
            if not warned and waited >= _LOCK_WARN_AFTER_S:
                _log.warning("Waiting on migration lock %s held by another process", lock_path)
                warned = True
            time.sleep(_LOCK_POLL_INTERVAL_S)
            waited += _LOCK_POLL_INTERVAL_S


def _release_migration_lock_sync(fd: IO[str]) -> None:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


@asynccontextmanager
async def _migration_lock(url: str):
    """Serialize the migration-and-backup section across processes.

    asyncio.Lock (_get_init_lock) only protects coroutines within this
    process; the CLI, the dashboard/scheduler server, and every `sova run`
    subprocess are separate processes that can all call init_db()/
    init_db_for_project() against the same .claude/sova.db at once, with only
    SQLite's busy timeout between them. flock() is released automatically if
    the holding process crashes, so there is no separate staleness timeout to
    implement. A no-op for non-file databases (in-memory tests, Postgres).
    """
    db_path = _get_db_path_from_url(url)
    if db_path is None:
        yield
        return

    # with_name (append), not with_suffix (replace): with_suffix would turn
    # "sova.db" into "sova.migrate.lock", dropping the ".db" and colliding with
    # any sibling SQLite file sharing that stem.
    lock_path = db_path.with_name(db_path.name + ".migrate.lock")
    fd = await asyncio.to_thread(_acquire_migration_lock_sync, lock_path)
    try:
        yield
    finally:
        await asyncio.to_thread(_release_migration_lock_sync, fd)


async def init_db(project_dir: Path | None = None, *, run_migrations: bool = True) -> None:
    """Initialize the database engine and ensure schema is current.

    The whole body runs under the same per-url ``_get_init_lock()`` guard
    ``init_db_for_project()`` uses: two concurrent callers for the same url
    (e.g. two requests racing ``get_session()`` right after startup, both
    finding ``_session_factory`` still None) would otherwise each create and
    install their own engine, with the second disposing the first's engine
    while it could still be mid-migration. There is no deadlock risk since
    this function never calls ``init_db_for_project()``.

    Args:
        project_dir: Project directory for DB path resolution.
        run_migrations: If True, run Alembic migrations. If False, use
            create_all (for tests with in-memory DBs).
    """
    global _engine, _session_factory

    url = _get_database_url(project_dir)
    connect_args: dict = {}

    db_path = _get_db_path_from_url(url)
    if url.startswith("sqlite"):
        # check_same_thread: aiosqlite runs in its own thread, not the caller's.
        # timeout: busy-wait up to 30 s when another connection holds the write
        # lock. Without this, concurrent access raises "database is locked" after
        # 5 s (the sqlite3 default) and crashes the request.
        connect_args = {"check_same_thread": False, "timeout": 30}
        if db_path is not None:
            _ensure_db_file_secured_before_open(db_path)

    async with _get_init_lock(url):
        previous_engine = _engine
        _engine = create_async_engine(url, connect_args=connect_args)
        _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        _engines[url] = (_engine, _session_factory)
        if (
            previous_engine is not None
            and _get_db_path_from_url(url) is not None
            and not any(e is previous_engine for e, _ in _engines.values())
        ):
            # Only dispose when genuinely unreachable AND file-backed: if
            # previous_engine is still a value in _engines (a different
            # project's url), get_session(project_dir=...) and
            # get_session_factory() still hold a factory bound to it, so
            # disposing here would pull the pool out from under them. An
            # in-memory SQLite engine's connection pool *is* the database
            # (see tests/conftest.py), so disposing it would destroy data a
            # concurrent caller may still be using rather than merely close a
            # reclaimable pool; only a file-backed engine has a pool worth
            # reclaiming this way.
            await previous_engine.dispose()

        if run_migrations:
            is_sqlite_file = db_path is not None

            async with _migration_lock(url):
                await _run_migrations(_engine)
                if is_sqlite_file:
                    # Pragmas (foreign_keys in particular) must not be registered
                    # until migrations are done: see _enable_sqlite_wal's docstring.
                    # Dispose first so any connection migrations opened pre-registration
                    # is evicted rather than reused pragma-less for this engine's
                    # lifetime; this also covers the old "stale schema cache after DDL"
                    # concern, so it is unconditional rather than gated on DDL having run.
                    await _engine.dispose()
                    await _enable_sqlite_wal(_engine)

            if is_sqlite_file and db_path is not None and db_path.exists():
                _secure_db_file(db_path)
        else:
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)


async def init_db_for_project(project_dir: Path) -> None:
    """Initialize a project-specific DB engine (multi-project mode)."""
    url = _get_database_url(project_dir)
    if url in _engines:
        return

    async with _get_init_lock(url):
        if url in _engines:
            return

        connect_args: dict = {}
        db_path = _get_db_path_from_url(url)
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
            if db_path is not None:
                _ensure_db_file_secured_before_open(db_path)

        engine = create_async_engine(url, connect_args=connect_args)

        is_sqlite_file = db_path is not None

        async with _migration_lock(url):
            await _run_migrations(engine)
            if is_sqlite_file:
                await engine.dispose()
                await _enable_sqlite_wal(engine)

        if is_sqlite_file and db_path is not None and db_path.exists():
            _secure_db_file(db_path)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        _engines[url] = (engine, factory)


async def get_session(project_dir: Path | None = None) -> AsyncSession:
    """Get an async database session."""
    if project_dir is not None:
        url = _get_database_url(project_dir)
        if url in _engines:
            _, factory = _engines[url]
            return factory()
        await init_db_for_project(project_dir)
        _, factory = _engines[url]
        return factory()

    from sova.config.context import get_project_dir

    ctx_dir = get_project_dir()
    if ctx_dir is not None:
        return await get_session(project_dir=ctx_dir)

    if _session_factory is None:
        await init_db()
    assert _session_factory is not None
    return _session_factory()


async def get_session_factory(project_dir: Path) -> async_sessionmaker:
    """Return the async_sessionmaker for a project (initializes DB if needed)."""
    url = _get_database_url(project_dir)
    if url not in _engines:
        await init_db_for_project(project_dir)
    _, factory = _engines[url]
    return factory


async def close_db() -> None:
    """Close all database engines."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None

    for engine, _ in list(_engines.values()):
        await engine.dispose()
    _engines.clear()

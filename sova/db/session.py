"""Async database session factory for SOVA.

Supports multi-project mode with per-project DB sessions.
Uses Alembic migrations for schema changes (not create_all).
Backs up SQLite databases when migrations apply DDL (not on every restart).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.db.models import Base

_engine = None
_session_factory = None
_engines: dict[str, tuple] = {}

_DB_FILENAME = "sova.db"
_log = logging.getLogger("sova.db")
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    """Lazily create the init lock (must be called inside a running event loop)."""
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


def _get_database_url(project_dir: Path | None = None) -> str:
    """Resolve the database URL.

    Priority:
    1. SOVA_DATABASE_URL env var (for PostgreSQL team deployments)
    2. SQLite file in project's .claude directory
    3. SQLite file in default location
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

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def _get_db_path_from_url(url: str) -> Path | None:
    """Extract the file path from a SQLite URL, or None for non-SQLite or in-memory."""
    if not url.startswith("sqlite"):
        return None
    path_str = url.split("///", 1)[-1] if "///" in url else None
    if path_str:
        path_str = path_str.split("?", 1)[0]
    if not path_str or path_str == ":memory:":
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


def _backup_db(url: str) -> Path | None:
    """Back up a SQLite database before migrations."""
    db_path = _get_db_path_from_url(url)
    if db_path is None or not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup = db_path.with_suffix(".db.bak")
    # Secure the destination before any content is written into it (bypassing
    # shutil.copy2's umask-based mode, which would leave a new file briefly
    # permissive mid-copy), so its contents are never exposed, not even during
    # the copy itself. An existing backup from before this ordering fix needs
    # an explicit chmod: O_CREAT|O_TRUNC does not change the mode of a file
    # that already exists.
    try:
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        os.chmod(backup, 0o600)
    shutil.copyfile(db_path, backup)
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


async def _run_migrations(engine) -> bool:
    """Run Alembic migrations programmatically.

    Handles four cases:
    1. Fresh DB (no tables): run all migrations from scratch
    2. Existing DB without alembic_version: stamp at current head (pre-Alembic DB)
    3. Existing DB with alembic_version at head: verify no ORM column is missing,
       then skip the upgrade (fast path)
    4. Existing DB with alembic_version behind head: upgrade to head

    Falls back to create_all + stamp if Alembic migration fails.

    Returns True if DDL was executed (caller should dispose pool to clear schema
    cache), False if the DB was already at head (pool reuse is safe).
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
            if row is None:
                await conn.run_sync(lambda c: c.execute(text("DROP TABLE alembic_version")))
                has_alembic = False
            else:
                current_version = row[0] or None

    has_tables = bool(set(table_names) - {"alembic_version"}) if not has_alembic else bool(table_names)

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

    try:
        if has_tables and not has_alembic:
            async with engine.begin() as conn:
                await conn.run_sync(_do_stamp)
        else:
            async with engine.begin() as conn:
                await conn.run_sync(_do_upgrade)
    except Exception:  # noqa: BLE001 (any migration failure falls back to create_all)
        _log.warning("Alembic migration failed, falling back to create_all", exc_info=True)
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

    ``journal_mode`` persists in the database file, but ``synchronous`` is
    per-connection, so it has to be set on each connect rather than once at
    startup. NORMAL is the correct setting under WAL: it never risks
    corruption (only the most recent transactions on an OS crash or power
    loss) and avoids an fsync on every commit. With FULL, a machine running
    several agents spends long enough inside each commit that other writers
    exhaust the 30 s busy timeout and fail with "database is locked".
    """
    from typing import Any

    from sqlalchemy import event
    from sqlalchemy.pool import ConnectionPoolEntry

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: ConnectionPoolEntry) -> None:
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
        except Exception:  # noqa: BLE001 (sqlite3 DB-API connection is opaque; pragma failure is advisory)
            _log.warning("Failed to set SQLite synchronous mode", exc_info=True)


async def _enable_sqlite_wal(engine) -> None:
    """Enable WAL journal mode on the first connection.

    WAL mode persists in the database file after the first set. Subsequent
    connections automatically use WAL without re-running the PRAGMA.
    The busy timeout is handled per-connection via connect_args={"timeout": 30},
    and ``synchronous`` via the connect listener registered above.
    """
    _register_sqlite_pragmas(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
    except Exception:  # noqa: BLE001 (WAL mode is an optimization; non-SQLite backends and locked DBs both fail here)
        _log.warning("Failed to set SQLite WAL mode", exc_info=True)


async def init_db(project_dir: Path | None = None, *, run_migrations: bool = True) -> None:
    """Initialize the database engine and ensure schema is current.

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

    _engine = create_async_engine(url, connect_args=connect_args)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    _engines[url] = (_engine, _session_factory)

    if run_migrations:
        is_sqlite_file = db_path is not None
        if is_sqlite_file:
            await _enable_sqlite_wal(_engine)

        ddl_executed = await _run_migrations(_engine)
        if is_sqlite_file and ddl_executed:
            # Alembic's synchronous DDL via run_sync can leave aiosqlite
            # connections with a stale schema cache. Only dispose when DDL
            # actually ran: skipping dispose saves one connection round-trip
            # (~300 ms) on every restart when the DB is already at head.
            _backup_db(url)
            await _engine.dispose()

        if db_path is not None and db_path.exists():
            _secure_db_file(db_path)
    else:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def init_db_for_project(project_dir: Path) -> None:
    """Initialize a project-specific DB engine (multi-project mode)."""
    url = _get_database_url(project_dir)
    if url in _engines:
        return

    async with _get_init_lock():
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
        if is_sqlite_file:
            await _enable_sqlite_wal(engine)

        ddl_executed = await _run_migrations(engine)
        if is_sqlite_file and ddl_executed:
            _backup_db(url)
            await engine.dispose()

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

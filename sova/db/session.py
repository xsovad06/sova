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
        db_path = project_dir / ".claude" / _DB_FILENAME
    else:
        db_path = Path.home() / ".config" / "sova" / _DB_FILENAME

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def _get_db_path_from_url(url: str) -> Path | None:
    """Extract the file path from a SQLite URL, or None for non-SQLite."""
    if not url.startswith("sqlite"):
        return None
    path_str = url.split("///", 1)[-1] if "///" in url else None
    return Path(path_str) if path_str else None


def _backup_db(url: str) -> Path | None:
    """Back up a SQLite database before migrations."""
    db_path = _get_db_path_from_url(url)
    if db_path is None or not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup = db_path.with_suffix(".db.bak")
    shutil.copy2(db_path, backup)
    return backup


_ALEMBIC_HEAD_CACHE: str | None = None


def _get_alembic_head(alembic_cfg) -> str | None:
    """Return the current head revision from migration scripts, or None on error.

    Result is cached for the process lifetime -- the head revision never changes
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
    except Exception:
        return None


async def _run_migrations(engine) -> bool:
    """Run Alembic migrations programmatically.

    Handles four cases:
    1. Fresh DB (no tables): run all migrations from scratch
    2. Existing DB without alembic_version: stamp at current head (pre-Alembic DB)
    3. Existing DB with alembic_version at head: skip upgrade (fast path)
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

    # Fast path: DB is already at migration head -- skip the upgrade entirely.
    # This saves ~300ms of Alembic script loading on every server restart.
    # Only safe when version is non-empty (empty string means corrupted tracking).
    if has_alembic and current_version:
        head = _get_alembic_head(alembic_cfg)
        if head is not None and current_version == head:
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
    except Exception:
        log = logging.getLogger("sova.db")
        log.warning("Alembic migration failed, falling back to create_all", exc_info=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: c.execute(text("DROP TABLE IF EXISTS alembic_version")))
                await conn.run_sync(_do_stamp)
        except Exception:
            log.warning("Alembic stamp also failed; tables created but untracked", exc_info=True)
    return True  # DDL was executed


async def _enable_sqlite_wal(engine) -> None:
    """Enable WAL journal mode on the first connection.

    WAL mode persists in the database file after the first set -- subsequent
    connections automatically use WAL without re-running the PRAGMA.
    The busy timeout is handled per-connection via connect_args={"timeout": 30}.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
    except Exception:
        log = logging.getLogger("sova.db")
        log.warning("Failed to set SQLite WAL mode", exc_info=True)


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

    if url.startswith("sqlite"):
        # check_same_thread: aiosqlite runs in its own thread, not the caller's.
        # timeout: busy-wait up to 30 s when another connection holds the write
        # lock. Without this, concurrent access raises "database is locked" after
        # 5 s (the sqlite3 default) and crashes the request.
        connect_args = {"check_same_thread": False, "timeout": 30}

    _engine = create_async_engine(url, connect_args=connect_args)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    _engines[url] = (_engine, _session_factory)

    if run_migrations:
        is_sqlite_file = _get_db_path_from_url(url) is not None
        if is_sqlite_file:
            await _enable_sqlite_wal(_engine)

        ddl_executed = await _run_migrations(_engine)
        if is_sqlite_file and ddl_executed:
            # Alembic's synchronous DDL via run_sync can leave aiosqlite
            # connections with a stale schema cache. Only dispose when DDL
            # actually ran -- skipping dispose saves one connection round-trip
            # (~300 ms) on every restart when the DB is already at head.
            _backup_db(url)
            await _engine.dispose()
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
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}

        engine = create_async_engine(url, connect_args=connect_args)

        is_sqlite_file = _get_db_path_from_url(url) is not None
        if is_sqlite_file:
            await _enable_sqlite_wal(engine)

        ddl_executed = await _run_migrations(engine)
        if is_sqlite_file and ddl_executed:
            _backup_db(url)
            await engine.dispose()

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

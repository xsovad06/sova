"""Async database session factory for SOVA.

Supports multi-project mode with per-project DB sessions.
Uses Alembic migrations for schema changes (not create_all).
Backs up SQLite databases before running migrations.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.db.models import Base

_engine = None
_session_factory = None
_engines: dict[str, tuple] = {}


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
        db_path = project_dir / ".claude" / "sova.db"
    else:
        db_path = Path.home() / ".config" / "sova" / "sova.db"

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


async def _run_migrations(engine) -> None:
    """Run Alembic migrations programmatically.

    Handles three cases:
    1. Fresh DB (no tables): run all migrations from scratch
    2. Existing DB without alembic_version: stamp at current head (pre-Alembic DB)
    3. Existing DB with alembic_version: upgrade to head

    Uses Alembic's MigrationContext directly to avoid event loop conflicts.
    """
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(Path(__file__).parent / "alembic.ini"))
    alembic_cfg.attributes["connection"] = None

    async with engine.connect() as conn:
        table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        has_tables = bool(table_names)
        has_alembic = "alembic_version" in table_names

    def _do_upgrade(sync_conn):
        alembic_cfg.attributes["connection"] = sync_conn
        command.upgrade(alembic_cfg, "head")

    def _do_stamp(sync_conn):
        alembic_cfg.attributes["connection"] = sync_conn
        command.stamp(alembic_cfg, "head")

    if has_tables and not has_alembic:
        async with engine.begin() as conn:
            await conn.run_sync(_do_stamp)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(_do_upgrade)


async def init_db(project_dir: Path | None = None, *, run_migrations: bool = True) -> None:
    """Initialize the database engine and ensure schema is current.

    Args:
        project_dir: Project directory for DB path resolution.
        run_migrations: If True, run Alembic migrations. If False, use
            create_all (for tests with in-memory DBs).
    """
    global _engine, _session_factory

    url = _get_database_url(project_dir)
    connect_args = {}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(url, connect_args=connect_args)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    if run_migrations:
        _backup_db(url)
        await _run_migrations(_engine)
    else:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def init_db_for_project(project_dir: Path) -> None:
    """Initialize a project-specific DB engine (multi-project mode)."""
    url = _get_database_url(project_dir)
    if url in _engines:
        return

    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_async_engine(url, connect_args=connect_args)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    _backup_db(url)
    await _run_migrations(engine)

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

    for _url, (engine, _) in list(_engines.items()):
        await engine.dispose()
    _engines.clear()

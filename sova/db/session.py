"""Async database session factory for SOVA."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.db.models import Base

_engine = None
_session_factory = None


def _get_database_url(project_dir: Path | None = None) -> str:
    """Resolve the database URL.

    Priority:
    1. SOVA_DATABASE_URL env var (for PostgreSQL team deployments)
    2. SQLite file in project's .claude directory
    3. SQLite file in default location
    """
    env_url = os.environ.get("SOVA_DATABASE_URL")
    if env_url:
        # Convert postgresql:// to postgresql+asyncpg://
        if env_url.startswith("postgresql://"):
            return env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return env_url

    if project_dir:
        db_path = project_dir / ".claude" / "sova.db"
    else:
        db_path = Path.home() / ".config" / "sova" / "sova.db"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


async def init_db(project_dir: Path | None = None) -> None:
    """Initialize the database engine and create tables."""
    global _engine, _session_factory

    url = _get_database_url(project_dir)
    connect_args = {}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(url, connect_args=connect_args)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Get an async database session."""
    if _session_factory is None:
        await init_db()
    assert _session_factory is not None
    return _session_factory()


async def close_db() -> None:
    """Close the database engine."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None

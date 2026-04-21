"""Alembic environment for SOVA migrations.

Supports two modes:
1. Programmatic (from session.py): connection passed via config.attributes
2. CLI (`alembic upgrade head`): creates its own async engine
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from sova.db.models import Base
from sova.db.session import _get_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without connecting to the database."""
    url = config.get_main_option("sqlalchemy.url") or _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    If a connection is passed via config.attributes (programmatic mode),
    use it directly. Otherwise create a new async engine (CLI mode).
    """
    connectable = config.attributes.get("connection")

    if connectable is not None:
        _do_run_migrations(connectable)
        return

    asyncio.run(_run_async_migrations())


async def _run_async_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url") or _get_database_url()
    connectable = create_async_engine(url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

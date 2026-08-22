"""Database-backed configuration persistence for SOVA.

Provides async CRUD functions for storing project settings in the DB
(key-value with JSON-serialized values and dot-notation keys), plus a
sync bridge for use inside the synchronous ``load_config()`` path.

Priority chain: env vars > DB > TOML > defaults.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import ProjectSetting

logger = logging.getLogger(__name__)

_DB_FILENAME = "sova.db"


async def load_config_from_db(session: AsyncSession) -> dict[str, Any] | None:
    """Load all project settings from the DB and return as a nested dict.

    Returns None if the table does not exist, is empty, or cannot be read.
    """
    try:
        result = await session.execute(select(ProjectSetting).order_by(ProjectSetting.key))
        rows = result.scalars().all()
    except (OperationalError, ProgrammingError):
        await session.rollback()
        return None
    except Exception:
        logger.warning("Failed to load config from DB", exc_info=True)
        await session.rollback()
        return None

    if not rows:
        return None

    return _rows_to_nested(rows)


async def save_setting(session: AsyncSession, key: str, value: Any) -> None:
    """Upsert a single setting (dot-notation key, any JSON-serializable value)."""
    from sqlalchemy.exc import IntegrityError

    json_value = json.dumps(value)
    existing = await session.execute(select(ProjectSetting).where(ProjectSetting.key == key))
    row = existing.scalar_one_or_none()
    if row is not None:
        row.value = json_value
    else:
        try:
            async with session.begin_nested():
                session.add(ProjectSetting(key=key, value=json_value))
                await session.flush()
            return
        except IntegrityError:
            result = await session.execute(select(ProjectSetting).where(ProjectSetting.key == key))
            row = result.scalar_one_or_none()
            if row is not None:
                row.value = json_value
    await session.flush()


async def delete_setting(session: AsyncSession, key: str) -> bool:
    """Delete a setting by key. Returns True if it existed."""
    existing = await session.execute(select(ProjectSetting).where(ProjectSetting.key == key))
    row = existing.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def get_setting(session: AsyncSession, key: str) -> Any | None:
    """Get a single setting value by dot-notation key. Returns None if missing."""
    try:
        result = await session.execute(select(ProjectSetting).where(ProjectSetting.key == key))
        row = result.scalar_one_or_none()
    except (OperationalError, ProgrammingError):
        await session.rollback()
        return None
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return None


def _rows_to_nested(rows: Any) -> dict[str, Any] | None:
    """Build a nested dict from key/JSON-value rows. Returns None when nothing is usable."""
    nested: dict[str, Any] = {}
    for row in rows:
        try:
            value = json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Skipping setting %r: invalid JSON value", row.key)
            continue
        _set_nested(nested, row.key, value)
    return nested if nested else None


def _set_nested(d: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using a dot-notation key.

    Example: _set_nested({}, "pipeline.auto_handoff", True)
             -> {"pipeline": {"auto_handoff": True}}
    """
    parts = dotted_key.split(".")
    current = d
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


async def save_task_queue(project_dir: Path | None, queue: list[int]) -> None:
    """Persist the supervisor task_queue to the DB.

    Shared by the dashboard API and the daemon's queue maintenance.
    Raises on failure so callers can handle rollback or HTTP errors.
    """
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        async with session.begin():
            await save_setting(session, "supervisor.task_queue", queue)


async def load_task_queue(project_dir: Path | None) -> list[int]:
    """Load the supervisor task_queue from the DB (async).

    Returns [] if not set or if the stored data is malformed.
    Validates that every element is a positive int (rejects bool subclass,
    zero, and negative values). Shared by dashboard API endpoints that need
    the current queue state without going through the sync load_config().
    """
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        queue = await get_setting(session, "supervisor.task_queue")
    if not isinstance(queue, list):
        return []
    if not all(type(x) is int and x > 0 for x in queue):
        logger.warning("Corrupted task_queue in DB, returning empty: %r", queue)
        return []
    return queue


async def save_config_to_db(session: AsyncSession, config: dict[str, Any]) -> None:
    """Bulk-save a flat config dict to the DB as ProjectSetting rows.

    Keys use dot-notation (e.g. ``task_source.type``).  Values are
    JSON-serialized.  Uses ``save_setting`` (upsert) so existing rows
    are updated and new rows are inserted.

    Stale keys under replaced sections are deleted: switching from Jira
    to GitHub removes orphaned ``task_source.jira_*`` rows.
    """
    flat = _flatten_config_dict(config)
    section_prefixes = [f"{k}." for k, v in config.items() if isinstance(v, dict)]
    if section_prefixes:
        result = await session.execute(select(ProjectSetting))
        for row in result.scalars().all():
            if any(row.key.startswith(p) for p in section_prefixes) and row.key not in flat:
                await session.delete(row)
        await session.flush()
    for key, value in flat.items():
        await save_setting(session, key, value)


def _save_config_to_db_sync(project_dir: Path, config: dict[str, Any]) -> None:
    """Sync bridge: save config dict to DB using a sync engine.

    Follows the same pattern as ``_try_load_from_db`` to avoid
    forcing ``asyncio.run()`` into CLI callers.
    """
    db_path = project_dir / ".claude" / _DB_FILENAME
    if not db_path.exists():
        logger.debug("DB file does not exist at %s, skipping sync save", db_path)
        return

    flat = _flatten_config_dict(config)
    if not flat:
        return

    try:
        from sqlalchemy import create_engine, text

        sync_url = f"sqlite:///{db_path}"
        connect_args: dict = {"check_same_thread": False, "timeout": 5}
        engine = create_engine(sync_url, connect_args=connect_args)
        try:
            with engine.begin() as conn:
                try:
                    conn.execute(text("SELECT 1 FROM project_settings LIMIT 0"))
                except (OperationalError, ProgrammingError):
                    logger.debug("project_settings table does not exist, skipping sync save")
                    return

                # Remove stale keys under replaced sections
                section_prefixes = [f"{k}." for k, v in config.items() if isinstance(v, dict)]
                if section_prefixes:
                    existing_rows = conn.execute(text("SELECT key FROM project_settings")).fetchall()
                    for row in existing_rows:
                        if any(row[0].startswith(p) for p in section_prefixes) and row[0] not in flat:
                            conn.execute(text("DELETE FROM project_settings WHERE key = :key"), {"key": row[0]})

                for key, value in flat.items():
                    json_value = json.dumps(value)
                    row = conn.execute(
                        text("SELECT id FROM project_settings WHERE key = :key"),
                        {"key": key},
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            text("UPDATE project_settings SET value = :value WHERE key = :key"),
                            {"key": key, "value": json_value},
                        )
                    else:
                        conn.execute(
                            text("INSERT INTO project_settings (key, value) VALUES (:key, :value)"),
                            {"key": key, "value": json_value},
                        )
        finally:
            engine.dispose()
    except Exception:
        logger.warning("Failed to save config to DB (sync)", exc_info=True)


def _flatten_config_dict(config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict into dot-notation keys for DB storage.

    Example: {"task_source": {"type": "github"}} -> {"task_source.type": "github"}
    Scalar top-level values are stored as-is: {"github_repo": "a/b"} -> {"github_repo": "a/b"}
    """
    flat: dict[str, Any] = {}
    for key, value in config.items():
        full_key = key if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten_config_dict(value, full_key))
        else:
            flat[full_key] = value
    return flat


def _resolve_db_path(project_dir: Path | None) -> Path | None:
    """Resolve the SQLite DB path without side effects (no mkdir).

    Returns None when an env-var URL is set (caller must use the full
    _get_database_url path) or when the DB file does not exist yet.
    """
    if os.environ.get("SOVA_DATABASE_URL"):
        return None

    if project_dir is not None:
        db_path = project_dir / ".claude" / _DB_FILENAME
    else:
        db_path = Path.home() / ".config" / "sova" / _DB_FILENAME

    return db_path if db_path.exists() else None


def _try_load_from_db(project_dir: Path | str | None) -> dict[str, Any] | None:
    """Sync bridge: load config from DB if possible.

    Returns None (silent fallback) when:
    - The DB file does not exist on disk
    - The table does not exist (pre-migration DB)
    - The DB is unreachable
    - Any other error
    """
    db_path = _resolve_db_path(Path(project_dir) if project_dir is not None else None)
    if db_path is None:
        return None

    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy import select as sa_select

        sync_url = f"sqlite:///{db_path}"
        connect_args: dict = {"check_same_thread": False, "timeout": 5}

        engine = create_engine(sync_url, connect_args=connect_args)
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(text("SELECT 1 FROM project_settings LIMIT 0"))
                except (OperationalError, ProgrammingError):
                    return None

                rows = conn.execute(
                    sa_select(ProjectSetting.__table__).order_by(ProjectSetting.__table__.c.key)
                ).fetchall()

            if not rows:
                return None

            return _rows_to_nested(rows)
        finally:
            engine.dispose()
    except Exception:
        logger.debug("DB config load skipped", exc_info=True)
        return None

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
            session.add(ProjectSetting(key=key, value=json_value))
            await session.flush()
            return
        except IntegrityError:
            await session.rollback()
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

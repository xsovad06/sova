"""Database-backed configuration persistence for SOVA.

Provides async CRUD functions for storing project settings in the DB
(key-value with JSON-serialized values and dot-notation keys), plus a
sync bridge for use inside the synchronous ``load_config()`` path.

Priority chain: env vars > DB > TOML > defaults.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import os
import threading
import time
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import ProjectSetting
from sova.db.session import _ORPHAN_SWEEP_MARKER_KEY, _get_database_url
from sova.utils.logging import get_logger

logger = get_logger(component="config.db_loader")

# Internal bookkeeping rows stored in project_settings that are not user-facing
# configuration and must never surface in the config dict returned to callers.
_INTERNAL_SETTING_KEYS = frozenset({_ORPHAN_SWEEP_MARKER_KEY})


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


async def delete_setting(session: AsyncSession, key: str, *, with_children: bool = False) -> int:
    """Delete the row for *key* and, with *with_children*, every ``key.<child>`` row under it.

    An object-valued setting such as ``llm.routing`` is stored one row per entry (``llm.routing.develop``),
    so removing it has to remove those rows too. Returns how many rows were deleted.
    """
    from sqlalchemy import delete, or_

    table = ProjectSetting.__table__
    condition = table.c.key == key
    if with_children:
        escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = or_(condition, table.c.key.like(f"{escaped}.%", escape="\\"))
    result = await session.execute(delete(table).where(condition))
    await session.flush()
    return int(result.rowcount or 0)


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
        if row.key in _INTERNAL_SETTING_KEYS:
            continue
        try:
            value = json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            # Rendered text is documented user-facing troubleshooting guidance
            # (CLAUDE.md, docs/troubleshooting-config.md): keep it verbatim.
            logger.warning(f"Skipping setting {row.key!r}: invalid JSON value")
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


TASK_QUEUE_KEY = "supervisor.task_queue"
HIDDEN_ISSUES_KEY = "dashboard.hidden_issues"

# Compare-and-swap rounds a merged queue write gets before giving up: each lost round means another writer
# saved the queue in between, so running out takes a sustained burst of writes from other processes.
_QUEUE_SWAP_ATTEMPTS = 8


class TaskQueueConflictError(RuntimeError):
    """Raised when every compare-and-swap round lost to a concurrent queue write; nothing was saved."""


def merge_task_queue(stored: Sequence[Any], based_on: Sequence[Any], queue: Sequence[Any]) -> list[Any]:
    """Rebase a writer's ``queue`` (computed from ``based_on``) onto the queue now ``stored``.

    The writer's order and its own removals and additions win. An entry another writer removed since
    ``based_on`` stays removed, and an entry another writer added since then is kept, after the writer's own.
    """
    merged = [entry for entry in queue if entry in stored or entry not in based_on]
    merged += [entry for entry in stored if entry not in based_on and entry not in queue]
    return merged


async def _swap_task_queue(project_dir: Path | None, rebuild: Callable[[Any], list[Any] | None]) -> list[Any] | None:
    """Save ``rebuild(stored)`` over the stored queue only if nobody wrote it since it was read; retry otherwise.

    The write is a conditional UPDATE matching the exact stored text (or an INSERT that the unique key rejects
    when another writer created the row first), so a writer in another process (the CLI's post-merge prune, the
    daemon, a second dashboard) can never be overwritten from a stale read: the losing writer re-reads and
    rebuilds. ``rebuild`` gets the decoded stored value (``None`` when unset or unreadable) and returns the new
    queue, or ``None`` to write nothing. Returns what was saved, or ``None`` when nothing was. Raises
    ``TaskQueueConflictError`` after ``_QUEUE_SWAP_ATTEMPTS`` lost rounds, and on a persistence failure.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from sova.config.loader import invalidate_config_cache
    from sova.db.session import get_session

    for _attempt in range(_QUEUE_SWAP_ATTEMPTS):
        async with await get_session(project_dir=project_dir) as session:
            result = await session.execute(select(ProjectSetting.value).where(ProjectSetting.key == TASK_QUEUE_KEY))
            raw = result.scalar_one_or_none()
        try:
            stored = json.loads(raw) if raw is not None else None
        except (json.JSONDecodeError, TypeError):
            stored = None
        updated = rebuild(stored)
        if updated is None:
            return None
        new_raw = json.dumps(updated)
        async with await get_session(project_dir=project_dir) as session, session.begin():
            if raw is None:
                try:
                    async with session.begin_nested():
                        session.add(ProjectSetting(key=TASK_QUEUE_KEY, value=new_raw))
                        await session.flush()
                    swapped = True
                except IntegrityError:
                    swapped = False
            else:
                statement = (
                    update(ProjectSetting)
                    .where(ProjectSetting.key == TASK_QUEUE_KEY, ProjectSetting.value == raw)
                    .values(value=new_raw, updated_at=datetime.now(timezone.utc))
                    .execution_options(synchronize_session=False)
                )
                swapped = (await session.execute(statement)).rowcount == 1
        if swapped:
            invalidate_config_cache(project_dir or Path.cwd())
            return updated
        logger.info("db_loader.task_queue_swap_lost", attempt=_attempt + 1)
    raise TaskQueueConflictError(f"supervisor.task_queue changed concurrently {_QUEUE_SWAP_ATTEMPTS} times")


async def save_task_queue(
    project_dir: Path | None, queue: list[int], *, based_on: Sequence[int] | None = None
) -> list[int]:
    """Persist the supervisor task_queue to the DB and return what was saved.

    Without ``based_on`` the stored queue is replaced outright (a deliberate full replace, such as the
    dashboard's reorder or clear). With ``based_on``, the queue the caller read before computing ``queue``,
    the write is rebased onto whatever is stored now (``merge_task_queue()``) and saved by compare-and-swap,
    so a concurrent writer's change, in this process or another, is kept rather than overwritten. Shared by
    the dashboard API and the daemon's queue maintenance. Raises on failure so callers can handle rollback or
    HTTP errors.
    """
    from sova.db.session import get_session

    if based_on is not None:

        def rebuild(stored: Any) -> list[Any]:
            if stored is None:
                # No stored queue (never saved, or unreadable) means no other writer removed anything: merging
                # against [] would read every based_on entry as a removal and save an empty queue.
                return list(queue)
            current = stored if isinstance(stored, list) else []
            return merge_task_queue(current, list(based_on), list(queue))

        saved = await _swap_task_queue(project_dir, rebuild)
        return saved if saved is not None else list(queue)

    async with await get_session(project_dir=project_dir) as session, session.begin():
        await save_setting(session, TASK_QUEUE_KEY, queue)

    from sova.config.loader import invalidate_config_cache

    invalidate_config_cache(project_dir or Path.cwd())
    return list(queue)


async def load_task_queue(project_dir: Path | None) -> list[int]:
    """Load the supervisor task_queue from the DB (async).

    Returns [] if not set or if the stored data is malformed.
    Validates that every element is a positive int (rejects bool subclass,
    zero, and negative values). Shared by dashboard API endpoints that need
    the current queue state without going through the sync load_config().
    """
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        queue = await get_setting(session, TASK_QUEUE_KEY)
    if not isinstance(queue, list):
        return []
    if not all(type(x) is int and x > 0 for x in queue):
        logger.warning("db_loader.task_queue_corrupted", queue=repr(queue))
        return []
    return queue


# Serializes the supervisor routes' read-modify-write of `supervisor.task_queue` in this process, so two route
# calls do not race each other. Writers that can run in another process (the CLI's post-merge prune, the
# daemon's maintenance) do not rely on it: they save through the compare-and-swap path (`_swap_task_queue()`),
# which needs no lock, so it is safe to call while holding this one.
task_queue_lock = asyncio.Lock()
_hidden_issues_lock = asyncio.Lock()


async def remove_from_task_queue(project_dir: Path | None, issue_ids: Collection[int]) -> list[int]:
    """Drop `issue_ids` from the stored `supervisor.task_queue`, keeping the order of the rest.

    Rebuilt from the stored queue and saved by compare-and-swap (``_swap_task_queue()``), so a caller with a
    stale view, in this process or another, can neither re-add an entry nor overwrite a concurrent edit, and
    a read that removes nothing never writes. An entry that is not an int is left alone rather than failing
    the prune. Returns the ids removed. Raises on a persistence failure.
    """
    wanted = {i for i in issue_ids if type(i) is int}
    if not wanted:
        return []
    removed: list[int] = []

    def rebuild(stored: Any) -> list[Any] | None:
        if not isinstance(stored, list):
            return None
        removed[:] = [entry for entry in stored if type(entry) is int and entry in wanted]
        if not removed:
            return None
        return [entry for entry in stored if not (type(entry) is int and entry in wanted)]

    kept = await _swap_task_queue(project_dir, rebuild)
    if kept is None:
        return []
    logger.info("db_loader.task_queue_pruned", removed=removed, queue_size=len(kept))
    return removed


def _positive_ids(stored: Any) -> list[int]:
    if not isinstance(stored, list):
        return []
    return list(dict.fromkeys(i for i in stored if type(i) is int and i > 0))


async def load_hidden_issues(project_dir: Path | None) -> list[int]:
    """The effective ``dashboard.hidden_issues`` (env, then DB, then TOML, then default), positive ints only.

    Read through ``load_config()`` rather than the DB alone, so a list set in ``sova.toml`` or by an environment
    override hides issues too. The load is synchronous (TOML and DB reads on a cache miss), so it runs in a thread.
    """
    from sova.config.loader import load_config

    config = await asyncio.to_thread(load_config, project_dir)
    return _positive_ids(config.dashboard.hidden_issues)


async def set_issue_hidden(project_dir: Path | None, issue_number: int, *, hidden: bool) -> list[int]:
    """Add or remove one issue in `dashboard.hidden_issues` and return the new list.

    Idempotent: hiding a hidden issue or unhiding a visible one changes and saves nothing. The current list is
    read and written in one transaction, and a failed read raises rather than reading as an empty list, so a
    database error can never replace the stored list with only this issue. Raises on a persistence failure.
    """
    from sova.config.loader import invalidate_config_cache
    from sova.db.session import get_session

    async with _hidden_issues_lock:
        async with await get_session(project_dir=project_dir) as session, session.begin():
            result = await session.execute(select(ProjectSetting.value).where(ProjectSetting.key == HIDDEN_ISSUES_KEY))
            raw = result.scalar_one_or_none()
            try:
                current = _positive_ids(json.loads(raw)) if raw is not None else []
            except (json.JSONDecodeError, TypeError):
                current = []  # an unreadable value holds no issue to keep
            if (issue_number in current) == hidden:
                return current
            updated = [*current, issue_number] if hidden else [i for i in current if i != issue_number]
            await save_setting(session, HIDDEN_ISSUES_KEY, updated)
        invalidate_config_cache(project_dir or Path.cwd())
    return updated


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


def _write_flat_config(conn: Any, config: dict[str, Any], flat: dict[str, Any]) -> None:
    """Write flat config entries into DB, deleting stale keys under replaced sections."""
    from sqlalchemy import text

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


def _save_with_immediate_lock(engine: Any, config: dict[str, Any], flat: dict[str, Any]) -> None:
    """Acquire a SQLite write lock via BEGIN IMMEDIATE before checking emptiness.

    Prevents a concurrent writer from inserting rows between the COUNT check
    and the migration writes.
    """
    from sqlalchemy import text

    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        try:
            conn.execute(text("SELECT 1 FROM project_settings LIMIT 0"))
        except (OperationalError, ProgrammingError):
            logger.debug("db_loader.project_settings_table_missing")
            return

        conn.execute(text("BEGIN IMMEDIATE"))
        try:
            row_count = conn.execute(text("SELECT COUNT(*) FROM project_settings")).fetchone()
            if row_count and row_count[0] > 0:
                logger.debug("db_loader.migration_skipped_not_empty", row_count=row_count[0])
                conn.execute(text("ROLLBACK"))
                return

            _write_flat_config(conn, config, flat)
            conn.execute(text("COMMIT"))
        except Exception:  # noqa: BLE001 (rolled back then re-raised; the caller decides how to handle it)
            conn.execute(text("ROLLBACK"))
            raise
    finally:
        conn.close()


def _save_within_transaction(engine: Any, config: dict[str, Any], flat: dict[str, Any]) -> None:
    """Standard save path using SQLAlchemy's managed transaction."""
    from sqlalchemy import text

    with engine.begin() as conn:
        try:
            conn.execute(text("SELECT 1 FROM project_settings LIMIT 0"))
        except (OperationalError, ProgrammingError):
            logger.debug("db_loader.project_settings_table_missing")
            return

        _write_flat_config(conn, config, flat)


def _save_config_to_db_sync(project_dir: Path, config: dict[str, Any], *, only_if_empty: bool = False) -> None:
    """Sync bridge: save config dict to DB using a sync engine.

    Follows the same pattern as ``_try_load_from_db`` to avoid
    forcing ``asyncio.run()`` into CLI callers.

    Args:
        project_dir: Project directory containing the DB.
        config: Nested config dict to flatten and save.
        only_if_empty: When True, migration is skipped if any rows exist (atomic check).
    """
    try:
        url = _sync_url_for(project_dir)
    except UnsupportedDatabaseError as exc:
        _warn_once(f"Configuration was not saved: {exc}")
        return
    db_file = _sqlite_file(url)
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite and (db_file is None or not db_file.exists()):
        logger.debug("db_loader.db_file_missing", project_dir=str(project_dir))
        return
    if only_if_empty and not is_sqlite:
        # The atomic emptiness check is BEGIN IMMEDIATE, a SQLite statement. Say so rather than
        # return as if the migration ran.
        _warn_once("Automatic migration of sova.toml into the database is only supported for SQLite; run it manually")
        return

    flat = _flatten_config_dict(config)
    if not flat:
        return

    try:
        from sqlalchemy import create_engine

        engine = create_engine(url, **_engine_kwargs(url))
        try:
            if only_if_empty:
                _save_with_immediate_lock(engine, config, flat)
            else:
                _save_within_transaction(engine, config, flat)
        finally:
            engine.dispose()
        touch_settings_stamp(project_dir)
    except Exception:  # noqa: BLE001 (config save is best-effort; the file-based config remains authoritative)
        logger.warning("db_loader.save_sync_failed", exc_info=True)


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


class UnsupportedDatabaseError(RuntimeError):
    """The configured database cannot be read by the synchronous config bridge.

    The message names the dialect or the missing package, never the URL, so it
    is safe to log and to return to a caller (the URL may carry a password).
    """


# Async driver name (as `sova.db.session._get_database_url` returns it) to the
# sync driver the config bridge opens. PostgreSQL names `psycopg` explicitly
# rather than relying on SQLAlchemy's default driver for a bare `postgresql://`.
_PG_SYNC_DRIVERNAME = "postgresql+psycopg"
_SYNC_DRIVERNAMES = {
    "sqlite": "sqlite",
    "sqlite+aiosqlite": "sqlite",
    "postgresql": _PG_SYNC_DRIVERNAME,
    "postgresql+asyncpg": _PG_SYNC_DRIVERNAME,
    _PG_SYNC_DRIVERNAME: _PG_SYNC_DRIVERNAME,
}

# PostgreSQL has no cheap file stamp, so a cached config is trusted for this long.
# In-process writers invalidate explicitly; a write from another process shows up
# within this window (docs/database-guidelines.md, "Config Cache").
PG_CONFIG_CACHE_TTL_S = 5.0

# A SQLite database is stamped by a small settings stamp file (see ``touch_settings_stamp``), which
# only a settings writer touches, so run output, TaskRun and cost writes to the same WAL database do
# not invalidate the cache. A writer that never touches it (the ``sqlite3`` shell) is picked up
# by this TTL instead.
SQLITE_CONFIG_CACHE_TTL_S = 60.0

# Suffix of the stamp file kept beside a SQLite database (``sova.db.settings-stamp``).
SETTINGS_STAMP_SUFFIX = ".settings-stamp"

# After a failed database read, the config served in its place is reused for a window before
# the next read attempt (one failed read per window, not one per call, during an outage). The
# window starts at the base, doubles with each consecutive failure up to the maximum, and
# resets on the first successful read. The base is longer than the sync connect timeout, so
# the event loop is blocked for a small fraction of the outage rather than about half of it.
DB_FAILURE_BACKOFF_S = 15.0
DB_FAILURE_BACKOFF_MAX_S = 120.0
PG_CONNECT_TIMEOUT_S = 2


def db_failure_backoff(consecutive_failures: int) -> float:
    """The backoff window after *consecutive_failures* failed reads in a row (1 for the first)."""
    exponent = max(consecutive_failures - 1, 0)
    return min(DB_FAILURE_BACKOFF_S * (2**exponent), DB_FAILURE_BACKOFF_MAX_S)


_warned_once: set[str] = set()
_warned_lock = threading.Lock()
_read_state = threading.local()


def _warn_once(message: str) -> None:
    """Log *message* at error level once per process (the same problem recurs on every load)."""
    with _warned_lock:
        if message in _warned_once:
            return
        _warned_once.add(message)
    logger.error(message)


@functools.lru_cache(maxsize=1)
def _psycopg_available() -> bool:
    """Whether the PostgreSQL sync driver is installed (cached: ``config_fingerprint`` asks on every load)."""
    return importlib.util.find_spec("psycopg") is not None


def _async_to_sync_url(url: str) -> URL:
    """Map a database URL from ``_get_database_url`` to the URL the sync bridge opens.

    Returns a ``URL`` object rather than a string so a password never has to be
    rendered into text. Raises ``UnsupportedDatabaseError`` for a dialect with
    no mapping, or for PostgreSQL when the sync driver is not installed.
    """
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise UnsupportedDatabaseError("SOVA_DATABASE_URL is not a valid database URL") from exc
    drivername = _SYNC_DRIVERNAMES.get(parsed.drivername)
    if drivername is None:
        raise UnsupportedDatabaseError(
            f"Database dialect {parsed.drivername!r} is not supported for configuration; "
            "settings stored there cannot be read back"
        )
    if drivername == _PG_SYNC_DRIVERNAME and not _psycopg_available():
        raise UnsupportedDatabaseError(
            "The PostgreSQL sync driver 'psycopg' is not installed, so settings stored in "
            "PostgreSQL cannot be read back. Install it with: pip install 'sova[postgres]'"
        )
    if drivername == _PG_SYNC_DRIVERNAME:
        parsed = _asyncpg_query_to_libpq(parsed)
    return parsed.set(drivername=drivername)


# asyncpg-only URL query keys: libpq (behind psycopg) rejects them, and they tune the async
# driver (statement cache, timeouts), not which database is read, so the sync URL drops them.
_ASYNCPG_ONLY_QUERY_KEYS = (
    "prepared_statement_cache_size",
    "prepared_statement_name_func",
    "statement_cache_size",
    "max_cached_statement_lifetime",
    "max_cacheable_statement_size",
    "command_timeout",
)

# asyncpg's ``ssl=<value>`` spelled as libpq's ``sslmode``.
_ASYNCPG_SSL_TO_SSLMODE = {
    "true": "require",
    "false": "disable",
    "disable": "disable",
    "allow": "allow",
    "prefer": "prefer",
    "require": "require",
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}


def _asyncpg_query_to_libpq(url: URL) -> URL:
    """Rewrite an asyncpg URL's query string into one libpq accepts.

    Translates ``ssl=<mode>`` to ``sslmode`` and drops asyncpg-only keys. A value that
    cannot be translated raises ``UnsupportedDatabaseError`` naming the parameter only
    (never the URL, which may carry a password).
    """
    query = dict(url.query)
    ssl = query.get("ssl")
    if ssl is not None:
        mode = _ASYNCPG_SSL_TO_SSLMODE.get(ssl.lower()) if isinstance(ssl, str) else None
        if mode is None:
            raise UnsupportedDatabaseError(
                "The 'ssl' parameter of SOVA_DATABASE_URL has a value that cannot be translated "
                "for the sync PostgreSQL driver; use sslmode=<mode> instead"
            )
        url = url.difference_update_query(["ssl"])
        if "sslmode" not in query:
            url = url.update_query_dict({"sslmode": mode})
    dropped = [key for key in _ASYNCPG_ONLY_QUERY_KEYS if key in query]
    return url.difference_update_query(dropped) if dropped else url


def _sync_url_for(project_dir: Path | None) -> URL:
    """The sync URL for the database ``get_session()`` writes to (same resolution, no mkdir)."""
    return _async_to_sync_url(_get_database_url(project_dir, create_dirs=False))


def _sqlite_file(url: URL) -> Path | None:
    """The file a sync SQLite URL names, or None for an in-memory database."""
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return None
    return Path(url.database)


def config_db_unreadable_reason(project_dir: Path | None) -> str | None:
    """Why settings written to this project's database could not be read back, else None.

    A write path calls this first: persisting a value that ``load_config()`` will
    never see must fail loudly rather than report success.
    """
    try:
        _sync_url_for(project_dir)
    except UnsupportedDatabaseError as exc:
        return str(exc)
    return None


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    """(mtime_ns, size, inode) of *path*, or None when it is absent (not an error)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def settings_stamp_file(project_dir: Path | None) -> Path | None:
    """The settings stamp file beside the project's SQLite database, or None (remote or in-memory database)."""
    try:
        url = _sync_url_for(project_dir)
    except UnsupportedDatabaseError:
        return None
    db_file = _sqlite_file(url)
    if db_file is None:
        return None
    return db_file.with_name(db_file.name + SETTINGS_STAMP_SUFFIX)


def touch_settings_stamp(project_dir: Path | None) -> None:
    """Mark the settings in a SQLite database as changed, so other processes rebuild their cached config.

    Call after the settings write has committed (``invalidate_config_cache`` does). The stamp is
    replaced rather than rewritten, so its inode changes even on a filesystem with coarse
    timestamps. A failure is logged and left to ``SQLITE_CONFIG_CACHE_TTL_S`` to heal.
    """
    stamp = settings_stamp_file(project_dir)
    if stamp is None or not stamp.parent.is_dir():
        return
    tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(str(time.time_ns()), encoding="utf-8")
        os.replace(tmp, stamp)
    except OSError:
        logger.warning("db_loader.settings_stamp_failed", exc_info=True)
        tmp.unlink(missing_ok=True)


def config_fingerprint(project_dir: Path) -> tuple[tuple[Any, ...], float | None]:
    """A cheap staleness stamp for ``load_config(project_dir)``, plus a cache TTL (None for no limit).

    Covers every input of the load that can change without this process knowing:
    ``sova.toml``, the ``SOVA_*`` environment, and the settings. A SQLite database is
    stamped by its inode (a replaced file) and its settings stamp file, never by the
    database or its ``-wal`` sidecar: in WAL mode every run-output, TaskRun or cost write
    changes those, which would make nearly every call on a busy server a cache miss.
    A remote database has no file to stamp, so it returns a short TTL instead; a SQLite
    database gets a long one as a backstop for a writer that does not touch the stamp.
    """
    env = hash(tuple(sorted((key, val) for key, val in os.environ.items() if key.upper().startswith("SOVA_"))))
    toml = _file_stamp(project_dir / "sova.toml")
    try:
        url = _sync_url_for(project_dir)
    except UnsupportedDatabaseError:
        return (env, toml, "unsupported"), None
    if url.get_backend_name() != "sqlite":
        return (env, toml, "remote", url.render_as_string(hide_password=True)), PG_CONFIG_CACHE_TTL_S
    db_file = _sqlite_file(url)
    if db_file is None:
        return (env, toml, "memory"), None
    db_stamp = _file_stamp(db_file)
    stamp = _file_stamp(db_file.with_name(db_file.name + SETTINGS_STAMP_SUFFIX))
    return (env, toml, str(db_file), db_stamp[2] if db_stamp else None, stamp), SQLITE_CONFIG_CACHE_TTL_S


def reset_db_read_failure() -> None:
    """Forget a previous failed DB read on this thread (call before a fresh config build)."""
    _read_state.failed = False


def db_read_failed() -> bool:
    """True when a DB read on this thread since the last reset hit an unexpected error.

    Missing database, missing table and an empty table are not failures: they
    mean "no stored settings". A locked or unreachable database is.
    """
    return bool(getattr(_read_state, "failed", False))


def _is_db_confirmed_empty(project_dir: Path | str | None) -> bool:
    """Return True only when the DB exists, the table exists, and it has zero rows.

    Returns False on any error or when the DB/table is unreachable (fail-closed).
    """
    try:
        url = _sync_url_for(Path(project_dir) if project_dir is not None else None)
    except UnsupportedDatabaseError:
        return False
    # Auto-migration is SQLite-only (`_save_config_to_db_sync(only_if_empty=True)` refuses
    # anything else), so a remote database is never probed: that connection would do nothing.
    if url.get_backend_name() != "sqlite":
        return False
    db_file = _sqlite_file(url)
    if db_file is None or not db_file.exists():
        return False

    try:
        from sqlalchemy import create_engine, inspect, text

        engine = create_engine(url, **_engine_kwargs(url))
        try:
            with engine.connect() as conn:
                if not inspect(conn).has_table(ProjectSetting.__tablename__):
                    return False
                row = conn.execute(text("SELECT COUNT(*) FROM project_settings")).fetchone()
                return row is not None and row[0] == 0
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 (fail-closed: docstring guarantees False on any error)
        logger.debug("db_config.emptiness_probe_failed", exc_info=True)
        return False


def _read_setting_rows(project_dir: Path | str | None, *, flag_failure: bool) -> list[Any] | None:
    """Every ``project_settings`` row, or None when they could not be read.

    A missing database file and a pre-migration database without the table hold no rows (``[]``). None is an
    unsupported dialect (logged once) or a read error, which *flag_failure* also records for ``db_read_failed()``.
    """
    try:
        url = _sync_url_for(Path(project_dir) if project_dir is not None else None)
    except UnsupportedDatabaseError as exc:
        _warn_once(str(exc))
        return None
    db_file = _sqlite_file(url)
    if url.get_backend_name() == "sqlite" and (db_file is None or not db_file.exists()):
        return []

    try:
        from sqlalchemy import create_engine, inspect
        from sqlalchemy import select as sa_select

        engine = create_engine(url, **_engine_kwargs(url))
        try:
            with engine.connect() as conn:
                # has_table, not a probe SELECT caught as OperationalError: a locked or dropped
                # connection must reach the handler below and be flagged, not read as "no table".
                if not inspect(conn).has_table(ProjectSetting.__tablename__):
                    return []

                return list(conn.execute(sa_select(ProjectSetting.__table__).order_by(ProjectSetting.__table__.c.key)))
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 (fallback to file-based config on any DB failure; flagged for the cache)
        if flag_failure:
            _read_state.failed = True
        logger.debug("db_loader.load_skipped", exc_info=True)
        return None


def _try_load_from_db(project_dir: Path | str | None) -> dict[str, Any] | None:
    """Sync bridge: load config from DB if possible.

    Reads the database ``get_session()`` writes to (``SOVA_DATABASE_URL`` first).
    Returns None (fallback to the non-DB layers) when:
    - The DB file does not exist on disk
    - The table does not exist (pre-migration DB)
    - The DB dialect or sync driver is unsupported (logged once as an error)
    - The DB is unreachable (flagged via ``db_read_failed()``)
    - Any other error
    """
    rows = _read_setting_rows(project_dir, flag_failure=True)
    if not rows:
        return None
    return _rows_to_nested(rows)


def read_stored_settings(project_dir: Path | str | None) -> dict[str, Any] | None:
    """The settings stored for this project as ``{dotted key: value}``, one entry per row, or None if unreadable.

    Internal bookkeeping rows are left out. A row whose value is not valid JSON is kept with its raw text, since
    it is still stored. An empty table is ``{}``; a missing database or table is ``{}`` too (nothing is stored).
    """
    rows = _read_setting_rows(project_dir, flag_failure=False)
    if rows is None:
        return None
    stored: dict[str, Any] = {}
    for row in rows:
        if row.key in _INTERNAL_SETTING_KEYS:
            continue
        try:
            stored[row.key] = json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            stored[row.key] = row.value
    return stored


def _engine_kwargs(url: URL) -> dict[str, Any]:
    """Keyword arguments for the sync bridge's engine (SQLite needs a thread and lock timeout)."""
    if url.get_backend_name() == "sqlite":
        return {"connect_args": {"check_same_thread": False, "timeout": 5}}
    if "connect_timeout" in url.query:
        return {}  # an explicit operator value is not clamped to the default
    return {"connect_args": {"connect_timeout": PG_CONNECT_TIMEOUT_S}}

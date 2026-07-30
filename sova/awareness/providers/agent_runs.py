"""AgentRunProvider: SOVA agent activity aggregation across registered projects.

Queries TaskRun records from each registered project's SQLite database
and surfaces them as awareness items: failures and human-handoff runs
as NEEDS_ATTENTION, in-progress and completed runs as INFORMATIONAL.

Follows the cross-project raw aiosqlite pattern from fleet_service.py:
read-only connections, schema guard, per-project timeout, Python-level
aggregation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.config.registry import list_projects
from sova.utils.logging import get_logger

_log = get_logger(component="awareness.agent_runs")

_DB_FILENAME = "sova.db"
_DEFAULT_LOOKBACK_HOURS = 24
_MAX_CONCURRENT = 5
_QUERY_TIMEOUT_SECONDS = 5.0
_ROW_LIMIT = 200

# Statuses that classify a run as NEEDS_ATTENTION with urgency 2.
_NEEDS_ATTENTION_STATUSES = frozenset({"failed", "rejected", "interrupted"})

# Statuses where a run is waiting on human action, classified as NEEDS_ATTENTION urgency 1.
_WAITING_STATUSES = frozenset({"awaiting_approval", "paused"})


class AgentRunProvider(AwarenessProvider):
    """Awareness provider for SOVA agent activity across registered projects."""

    name = "agent_runs"
    display_name = "SOVA Agent Runs"

    async def is_configured(self) -> bool:
        """Return True if at least one project is registered."""
        return bool(list_projects())

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        """Fetch agent run awareness items from all registered projects."""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=_DEFAULT_LOOKBACK_HOURS)

        registry = list_projects()
        if not registry:
            return []

        queryable = _partition_projects(registry)
        if not queryable:
            return []

        sem = asyncio.Semaphore(_MAX_CONCURRENT)
        tasks = [_safe_query(slug, db_path, since, sem) for slug, db_path in queryable]
        results = await asyncio.gather(*tasks)

        items: list[AwarenessItem] = []
        for result in results:
            if result:
                items.extend(result)

        return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _partition_projects(registry: dict[str, str]) -> list[tuple[str, Path]]:
    """Return (slug, db_path) pairs for projects that have a DB file."""
    queryable: list[tuple[str, Path]] = []
    for slug, path_str in registry.items():
        db_path = Path(path_str) / ".claude" / _DB_FILENAME
        if not db_path.exists():
            _log.debug("agent_runs.db_missing", slug=slug, path=str(db_path))
        else:
            queryable.append((slug, db_path))
    return queryable


async def _safe_query(
    slug: str,
    db_path: Path,
    since: datetime,
    sem: asyncio.Semaphore,
) -> list[AwarenessItem]:
    """Query one project, returning [] on any failure."""
    async with sem:
        try:
            return await asyncio.wait_for(
                _query_project(slug, db_path, since),
                timeout=_QUERY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _log.warning("agent_runs.query_timeout", slug=slug)
            return []
        except Exception:
            _log.warning("agent_runs.query_failed", slug=slug, exc_info=True)
            return []


async def _query_project(slug: str, db_path: Path, since: datetime) -> list[AwarenessItem]:
    """Open a read-only aiosqlite connection and return AwarenessItems."""
    timeout_ms = int(_QUERY_TIMEOUT_SECONDS * 1000)
    uri = db_path.as_uri() + "?mode=ro"

    async with aiosqlite.connect(uri, uri=True, timeout=_QUERY_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        await db.execute("PRAGMA query_only = ON")

        # Schema guard: skip old installs without task_runs
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'") as cur:
            if await cur.fetchone() is None:
                _log.debug("agent_runs.no_task_runs_table", slug=slug)
                return []

        # SQLAlchemy stores started_at in SQLite without timezone suffix and with a space
        # separator (e.g. "2026-07-29 21:34:57.123456"). isoformat() would produce a T
        # separator and +00:00 suffix, causing lexicographic comparison to fail near the
        # lookback boundary. Use strftime to match the actual storage format.
        since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        sql = """
            SELECT id, issue_number, role, status, pr_number,
                   handoff_json, error_message, started_at
            FROM task_runs
            WHERE started_at >= ?
            ORDER BY started_at DESC
            LIMIT ?
        """
        async with db.execute(sql, (since_str, _ROW_LIMIT)) as cur:
            rows = await cur.fetchall()

    if len(rows) == _ROW_LIMIT:
        _log.warning("agent_runs.row_limit_reached", slug=slug, limit=_ROW_LIMIT)

    return [_build_item(slug, row) for row in rows]


def _build_item(slug: str, row: aiosqlite.Row) -> AwarenessItem:
    """Convert a task_runs row into an AwarenessItem."""
    run_id: int = row["id"]
    issue_number: str | None = row["issue_number"]
    role: str = row["role"] or "unknown"
    status: str = row["status"] or "unknown"
    pr_number: int | None = row["pr_number"]
    error_message: str | None = row["error_message"]
    started_at_str: str = row["started_at"]

    handoff: dict = _parse_handoff(row["handoff_json"])

    category, urgency = _classify(status, handoff)
    outcome = _extract_outcome(status, pr_number, handoff, error_message)

    issue_label = f"#{issue_number}" if issue_number else "(no issue)"
    title = f"{role.capitalize()} {status} - {issue_label}"

    timestamp = _parse_timestamp(started_at_str)

    return AwarenessItem(
        id=f"agent_run:{slug}:{run_id}",
        provider="agent_runs",
        category=category,
        title=title,
        body=outcome,
        source_url="",
        timestamp=timestamp,
        urgency=urgency,
        action_hint=_action_hint(category),
        metadata={
            "project": slug,
            "run_id": run_id,
            "role": role,
            "status": status,
            "pr_number": pr_number,
            "issue_number": issue_number,
        },
    )


def _classify(status: str, handoff: dict) -> tuple[ItemCategory, int]:
    """Return (category, urgency) for a run."""
    if status in _NEEDS_ATTENTION_STATUSES:
        return ItemCategory.NEEDS_ATTENTION, 2
    if status in _WAITING_STATUSES:
        return ItemCategory.NEEDS_ATTENTION, 1
    if status == "done" and handoff.get("needs_human"):
        return ItemCategory.NEEDS_ATTENTION, 1
    return ItemCategory.INFORMATIONAL, 0


_TERMINAL_STATUSES = frozenset({"done", "failed", "rejected", "interrupted", "paused", "awaiting_approval"})


def _extract_outcome(
    status: str,
    pr_number: int | None,
    handoff: dict,
    error_message: str | None,
) -> str:
    """Build a human-readable outcome summary."""
    if status in _NEEDS_ATTENTION_STATUSES:
        if error_message:
            return error_message[:120]
        return f"Run {status}"

    if status in _WAITING_STATUSES:
        return status.replace("_", " ").capitalize()

    if status not in _TERMINAL_STATUSES:
        return "In progress"

    # Completed run: PR number takes priority.
    if pr_number is not None:
        return f"PR #{pr_number} created"

    next_action: str = handoff.get("next_action", "")
    if next_action:
        return next_action[:80]

    return "Completed"


def _parse_handoff(raw: str | None) -> dict:
    """Parse handoff_json column value, returning {} on any failure."""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO datetime string, attaching UTC if naive."""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _action_hint(category: ItemCategory) -> str:
    if category == ItemCategory.NEEDS_ATTENTION:
        return "Review run details"
    return ""


# ---------------------------------------------------------------------------
# Self-register
# ---------------------------------------------------------------------------

register_provider("agent_runs", AgentRunProvider)

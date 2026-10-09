"""Queue service -- priority-sorted open issues from the task adapter."""

from __future__ import annotations

import re
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import Task, TaskState
from sova.utils.formatting import iso_utc
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.queue")

# Priority mapping (lower = higher priority), same as scheduler
_STATE_PRIORITY: dict[TaskState, int] = {
    TaskState.IN_REVIEW: -1,
    TaskState.RESEARCHED: 0,
    TaskState.IN_PROGRESS: 1,
    TaskState.TRIAGED: 2,
    TaskState.BACKLOG: 3,
    TaskState.NEEDS_SPEC: 4,
    TaskState.HUMAN_ONLY: 5,
}

_ACTIONABLE_STATES = frozenset(
    {
        TaskState.BACKLOG,
        TaskState.TRIAGED,
        TaskState.RESEARCHED,
        TaskState.IN_PROGRESS,
        TaskState.IN_REVIEW,
        TaskState.NEEDS_SPEC,
        TaskState.HUMAN_ONLY,
    }
)

VALID_STATES_FOR_ACTION: dict[str, frozenset[TaskState]] = {
    "triage": frozenset({TaskState.BACKLOG}),
    "harden": frozenset({TaskState.BACKLOG, TaskState.TRIAGED, TaskState.NEEDS_SPEC}),
    "run": frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS}),
}

# Recommended action per state (RESEARCHED resolved dynamically based on spec status)
_RECOMMENDED_ACTION: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "research",
    TaskState.RESEARCHED: "review_spec",
    TaskState.IN_PROGRESS: "resume",
    TaskState.IN_REVIEW: "review",
    TaskState.NEEDS_SPEC: "research",
    TaskState.HUMAN_ONLY: "triage",
}

_PRIORITY_LABEL_ORDER: dict[str, int] = {
    "priority:critical": 0,
    "priority:high": 1,
    "priority:medium": 2,
    "priority:low": 3,
}


def _milestone_badge(milestone: str) -> str:
    """Extract short badge label from milestone title (e.g. 'P3: v0.1 ...' -> 'P3')."""
    if not milestone:
        return "--"
    return milestone.split(":")[0].strip()[:8]


_JIRA_PRIORITY_ORDER: dict[str, int] = {
    "Blocker": 0,
    "Critical": 0,
    "Highest": 0,
    "Major": 1,
    "High": 1,
    "Normal": 1,
    "Medium": 1,
    "Minor": 2,
    "Low": 2,
    "Trivial": 3,
    "Lowest": 3,
}


def _extract_label_priority(labels: list[str]) -> int:
    """Extract numeric priority from priority: labels. Lower = higher priority.

    Handles both spaced ("priority: high") and compact ("priority:high") formats.
    """
    for label in labels:
        normalized = label.replace(" ", "")
        if normalized in _PRIORITY_LABEL_ORDER:
            return _PRIORITY_LABEL_ORDER[normalized]
    return 99


_PHASE_RE = re.compile(r"(?:Phase\s*|P)(\d+)", re.IGNORECASE)

_QUEUE_CACHE_TTL = 120  # seconds
# Caches only the adapter's sorted task list (the GitHub/Jira round trip). The
# local enrichment (last_run from the DB, spec_status from spec files) is
# rebuilt on every call: baking it into the cached entry hid a just-finished
# run's status (e.g. awaiting_approval) for up to the full TTL (#1149).
_queue_cache: dict[str, tuple[float, list[Task]]] = {}
# Per-key generation counter, bumped on every invalidation and never reset.
# A fetch in flight when invalidation fires can still land afterward; it
# compares its captured generation before caching so it can't resurrect the
# pre-invalidation result and undo the invalidation for the rest of the TTL.
_queue_cache_generation: dict[str, int] = {}


def invalidate_queue_cache(project_dir: Path) -> None:
    """Drop the cached task list for a project so the next call refetches it.

    Called when an agent finishes, since an agent often changes the labels the
    queue's states come from. Cache keys are whatever path the caller passed
    (or "" when none), while agents carry a resolved path, so entries are
    matched by resolved path and the "" single-project entry is always dropped.
    """
    target = project_dir.resolve()
    keys = set(_queue_cache) | set(_queue_cache_generation)
    for key in keys:
        if key:
            try:
                if Path(key).resolve() != target:
                    continue
            except (OSError, RuntimeError):
                continue
        _queue_cache.pop(key, None)
        _queue_cache_generation[key] = _queue_cache_generation.get(key, 0) + 1


def _extract_phase_order(milestone: str | None) -> int:
    """Extract phase number from milestone title. Lower = earlier phase.

    Supports: 'Phase 1: Ship It', 'Phase 2', 'P1: ...', 'P2'.
    """
    if not milestone:
        return 99
    m = _PHASE_RE.match(milestone.strip())
    return int(m.group(1)) if m else 99


async def get_priority_queue(project_dir: Path | None = None) -> list[dict]:
    """Fetch open issues and return a priority-sorted queue.

    Uses the GitHub adapter via the project's SOVA config.
    Returns a list of dicts with: priority, issue, title, state, action, labels, url, last_run.
    The adapter's task list is cached for 120 seconds per project to reduce API
    calls; last_run and spec_status are local reads, refreshed on every call.
    """
    actionable = await _get_actionable_tasks(project_dir)
    if not actionable:
        return []

    last_runs = await _get_last_runs_by_issue(project_dir)

    queue = []
    for t in actionable:
        priority = _STATE_PRIORITY.get(t.state, 99)
        phase_order = _extract_phase_order(t.milestone)
        queue.append(
            {
                "issue": t.id,
                "title": t.title,
                "state": t.state.value,
                "priority": priority,
                "priority_label": _milestone_badge(t.milestone),
                "phase_order": phase_order,
                "action": _RECOMMENDED_ACTION.get(t.state, "triage"),
                # Copied, not aliased: t.labels/assignees/components are the
                # cached Task's own lists, and a caller mutating the queue
                # result in place would otherwise corrupt later cache hits.
                "labels": list(t.labels),
                "url": t.url,
                "last_run": last_runs.get(t.id),
                "created_at": t.metadata.get("created_at", ""),
                "assignees": list(t.assignees),
                "issue_type": t.issue_type,
                "jira_status": t.metadata.get("status", ""),
                "jira_priority": t.metadata.get("jira_priority", ""),
                "jira_key": t.metadata.get("key", ""),
                "story_points": t.story_points,
                "sprint": t.sprint,
                "components": list(t.components),
                "updated_at": t.metadata.get("updated_at", ""),
            }
        )

    await _enrich_spec_status(queue, project_dir)
    return queue


async def _get_actionable_tasks(project_dir: Path | None) -> list[Task]:
    """Return the priority-sorted actionable tasks from the adapter, cached per project."""
    cache_key = str(project_dir or "")
    now = time.monotonic()
    cached = _queue_cache.get(cache_key)
    if cached and (now - cached[0]) < _QUEUE_CACHE_TTL:
        return cached[1]
    # setdefault, not get: the key must exist in the dict *before* the await
    # below so a concurrent invalidate_queue_cache() can see and bump it.
    generation = _queue_cache_generation.setdefault(cache_key, 0)

    from sova.adapters import create_adapter
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (queue is empty rather than broken when config cannot be loaded)
        log.debug("queue.config_load_failed", exc_info=True)
        return []

    if cfg.task_source.type == "github" and not cfg.github_repo:
        return []

    from sova.adapters.base import TaskFilters

    try:
        adapter = create_adapter(cfg)
        tasks = await adapter.list_tasks(TaskFilters(paginate=True))
    except Exception as e:  # noqa: BLE001 (adapter construction and task listing each fail differently)
        log.warning("queue.fetch_tasks_failed", error=str(e), exc_info=True)
        _queue_cache.pop(cache_key, None)
        return []

    from sova.supervisor.github_quota import get_github_quota_tracker

    if not tasks and get_github_quota_tracker(cfg.github_user).should_skip():
        log.warning("queue.empty_due_to_rate_limit")
        _queue_cache.pop(cache_key, None)
        return []

    actionable = [t for t in tasks if t.state in _ACTIONABLE_STATES]
    actionable.sort(
        key=lambda t: (
            _STATE_PRIORITY.get(t.state, 99),
            _extract_phase_order(t.milestone),
            _extract_label_priority(t.labels),
            _JIRA_PRIORITY_ORDER.get(t.metadata.get("jira_priority", ""), 99),
            t.metadata.get("created_at", "9999"),
        )
    )

    # Only cache if no invalidation landed while this fetch was in flight;
    # otherwise the fetch predates the invalidation trigger and caching it
    # would resurrect stale data for the rest of the TTL.
    if _queue_cache_generation.get(cache_key, 0) == generation:
        _queue_cache[cache_key] = (time.monotonic(), actionable)
    return actionable


async def _enrich_spec_status(queue: list[dict], project_dir: Path | None) -> None:
    """Add spec_status and resolve action for RESEARCHED issues.

    Reads spec files to determine approval state. Updates the action
    field so RESEARCHED issues with an approved spec show "develop"
    while those with a draft spec show "review_spec".

    Runs synchronous file I/O via asyncio.to_thread to avoid blocking
    the event loop.
    """
    import asyncio

    def _enrich_sync() -> None:
        from sova.dashboard.services.spec_service import read_spec

        for item in queue:
            if item["state"] != "researched":
                item["spec_status"] = None
                continue

            try:
                spec = read_spec(item["issue"], project_dir=project_dir)
            except (OSError, ValueError):
                log.warning("Failed to read spec for issue %s", item["issue"], exc_info=True)
                item["spec_status"] = "missing"
                item["action"] = "develop"
                continue

            if spec is None:
                item["spec_status"] = "missing"
                item["action"] = "develop"
            else:
                item["spec_status"] = spec.get("status", "draft")
                if item["spec_status"] == "approved":
                    item["action"] = "develop"
                else:
                    item["action"] = "review_spec"

    await asyncio.to_thread(_enrich_sync)


async def _get_last_runs_by_issue(project_dir: Path | None) -> dict[str, dict]:
    """Get the most recent TaskRun for each issue number.

    Uses a subquery to find max(id) per issue, avoiding loading all rows.
    Also finds pr_number across ALL runs (reviewer runs often lack it).
    """
    try:
        from sqlalchemy import func, select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                latest_ids = select(func.max(TaskRun.id).label("max_id")).group_by(TaskRun.issue_number).subquery()
                stmt = select(TaskRun).where(TaskRun.id.in_(select(latest_ids.c.max_id)))
                result = await session.execute(stmt)
                runs = result.scalars().all()

                pr_stmt = (
                    select(TaskRun.issue_number, func.max(TaskRun.pr_number).label("pr"))
                    .where(TaskRun.pr_number.isnot(None))
                    .group_by(TaskRun.issue_number)
                )
                pr_result = await session.execute(pr_stmt)
                pr_by_issue = {row.issue_number: row.pr for row in pr_result}

        return {
            r.issue_number: {
                "id": r.id,
                "status": r.status,
                "role": r.role,
                "pr_number": r.pr_number or pr_by_issue.get(r.issue_number),
                "ended_at": iso_utc(r.ended_at),
                "started_at": iso_utc(r.started_at),
            }
            for r in runs
        }
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("Failed to fetch last runs for queue enrichment", exc_info=True)
        return {}

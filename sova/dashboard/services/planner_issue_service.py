"""Create tracker issues from planner-proposed tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from sova.adapters.label_catalog import AREA_CHOICES
from sova.adapters.priority import Priority
from sova.config.loader import load_config
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.planner_issues")

_CREATE_CONCURRENCY = 4


class PlannedTask(Protocol):
    """The fields of a planner-proposed task that issue creation reads."""

    title: str
    description: str
    component: str
    priority: str


def planner_task_labels(task: PlannedTask, *, is_github: bool) -> list[str] | None:
    """Build tracker-specific labels for a planner-proposed task.

    GitHub-style 'area: x' / 'priority: y' labels contain spaces Jira
    rejects outright, and Jira derives priority from its own native field,
    so only a GitHub-backed project gets labels built here at all.
    """
    if not is_github:
        return None
    labels = []
    if task.component:
        component = task.component.strip().lower()
        if component in AREA_CHOICES:
            labels.append(f"area: {component}")
        else:
            log.warning("planner.unknown_component", component=task.component)
    priority = planner_task_priority(task)
    if priority is not Priority.UNDEFINED:
        labels.append(priority.github_label)
    return labels or None


def planner_task_priority(task: PlannedTask) -> Priority:
    """The planner's priority for a task; UNDEFINED (with a warning for an unknown value) when it has none.

    GitHub carries it as a label (``planner_task_labels``) and Jira in its native priority field. UNDEFINED
    leaves Jira's default, since no name in a Jira scheme means "undefined".
    """
    if not task.priority or not task.priority.strip():
        return Priority.UNDEFINED
    try:
        return Priority(task.priority.strip().lower())
    except ValueError:
        log.warning("planner.unknown_priority", priority=task.priority)
        return Priority.UNDEFINED


async def create_planner_issues(tasks: Sequence[PlannedTask]) -> list[dict]:
    """Create one tracker issue per task and return one result per task, in order.

    A created item is ``{"ok": True, "number", "url", "title", "dropped_labels"}``; a failed one is
    ``{"ok": False, "title", "error": "Issue creation failed"}`` (a fixed reason: the cause is logged, never
    returned). ``title`` is always the title the task was submitted with (not the tracker's echo of it), so a caller
    can match a result to its proposal. One failure never aborts the batch.
    """
    from sova.adapters import create_adapter
    from sova.config.context import resolve_project_dir_or_none
    from sova.dashboard.services.agent_pool import get_default_project_dir

    if not tasks:
        return []

    cfg = load_config(resolve_project_dir_or_none(default_project_dir=get_default_project_dir()))
    adapter = create_adapter(cfg)
    is_github = cfg.task_source.type == "github"
    sem = asyncio.Semaphore(_CREATE_CONCURRENCY)

    async def _create_one(task: PlannedTask) -> dict:
        try:
            async with sem:
                result = await adapter.create_issue(
                    title=task.title,
                    body=task.description,
                    labels=planner_task_labels(task, is_github=is_github),
                    priority=Priority.UNDEFINED if is_github else planner_task_priority(task),
                )
            return {
                "ok": True,
                "number": result.id,
                "url": result.url,
                "title": task.title,
                "dropped_labels": result.metadata.get("dropped_labels", []),
            }
        except Exception:  # noqa: BLE001 (one failed issue must not abort the rest of the batch)
            log.warning("planner.create_issue_failed", title=task.title, exc_info=True)
            return {"ok": False, "title": task.title, "error": "Issue creation failed"}

    return list(await asyncio.gather(*[_create_one(t) for t in tasks]))

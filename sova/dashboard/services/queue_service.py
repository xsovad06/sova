"""Queue service -- priority-sorted open issues from the task adapter."""

from __future__ import annotations

from pathlib import Path

from sova.adapters.base import TaskState
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.queue")

# Priority mapping (lower = higher priority), same as scheduler
_STATE_PRIORITY: dict[TaskState, int] = {
    TaskState.RESEARCHED: 0,
    TaskState.IN_PROGRESS: 1,
    TaskState.TRIAGED: 2,
    TaskState.BACKLOG: 3,
}

_ACTIONABLE_STATES = frozenset({
    TaskState.BACKLOG,
    TaskState.TRIAGED,
    TaskState.RESEARCHED,
    TaskState.IN_PROGRESS,
})

# Recommended action per state
_RECOMMENDED_ACTION: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "research",
    TaskState.RESEARCHED: "develop",
    TaskState.IN_PROGRESS: "resume",
}


async def get_priority_queue(project_dir: Path | None = None) -> list[dict]:
    """Fetch open issues and return a priority-sorted queue.

    Uses the GitHub adapter via the project's sova.toml config.
    Returns a list of dicts with: priority, issue, title, state, action, labels, url.
    """
    from sova.adapters import create_adapter
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:
        log.debug("No config found for queue, returning empty")
        return []

    repo = cfg.github_repo
    if not repo:
        return []

    try:
        adapter = create_adapter(cfg.task_source.type, repo)
        tasks = await adapter.list_tasks()
    except Exception as e:
        log.warning("Failed to fetch tasks for queue: %s", e)
        return []

    actionable = [t for t in tasks if t.state in _ACTIONABLE_STATES]
    actionable.sort(key=lambda t: _STATE_PRIORITY.get(t.state, 99))

    queue = []
    for t in actionable:
        priority = _STATE_PRIORITY.get(t.state, 99)
        queue.append({
            "issue": t.id,
            "title": t.title,
            "state": t.state.value,
            "priority": priority,
            "priority_label": f"P{priority}",
            "action": _RECOMMENDED_ACTION.get(t.state, "triage"),
            "labels": t.labels,
            "url": t.url,
        })

    return queue

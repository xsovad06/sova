"""Queue service -- priority-sorted open issues from the task adapter."""

from __future__ import annotations

from pathlib import Path

from sova.adapters.base import TaskState
from sova.utils.formatting import iso_utc
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.queue")

# Priority mapping (lower = higher priority), same as scheduler
_STATE_PRIORITY: dict[TaskState, int] = {
    TaskState.RESEARCHED: 0,
    TaskState.IN_PROGRESS: 1,
    TaskState.TRIAGED: 2,
    TaskState.BACKLOG: 3,
}

_ACTIONABLE_STATES = frozenset(
    {
        TaskState.BACKLOG,
        TaskState.TRIAGED,
        TaskState.RESEARCHED,
        TaskState.IN_PROGRESS,
    }
)

VALID_STATES_FOR_ACTION: dict[str, frozenset[TaskState]] = {
    "triage": frozenset({TaskState.BACKLOG}),
    "harden": frozenset({TaskState.BACKLOG, TaskState.TRIAGED, TaskState.NEEDS_SPEC}),
    "run": frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS}),
}

# Recommended action per state
_RECOMMENDED_ACTION: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "research",
    TaskState.RESEARCHED: "develop",
    TaskState.IN_PROGRESS: "resume",
}

_PRIORITY_LABEL_ORDER: dict[str, int] = {
    "priority:critical": 0,
    "priority:high": 1,
    "priority:medium": 2,
    "priority:low": 3,
}


def _extract_label_priority(labels: list[str]) -> int:
    """Extract numeric priority from priority: labels. Lower = higher priority."""
    for label in labels:
        if label in _PRIORITY_LABEL_ORDER:
            return _PRIORITY_LABEL_ORDER[label]
    return 99


async def get_priority_queue(project_dir: Path | None = None) -> list[dict]:
    """Fetch open issues and return a priority-sorted queue.

    Uses the GitHub adapter via the project's sova.toml config.
    Returns a list of dicts with: priority, issue, title, state, action, labels, url, last_run.
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
        ts = cfg.task_source
        adapter = create_adapter(ts.type, repo, cfg.github_user, ts.github_project_number)
        tasks = await adapter.list_tasks()
    except Exception as e:
        log.warning("Failed to fetch tasks for queue: %s", e)
        return []

    actionable = [t for t in tasks if t.state in _ACTIONABLE_STATES]
    actionable.sort(
        key=lambda t: (
            _STATE_PRIORITY.get(t.state, 99),
            _extract_label_priority(t.labels),
            t.metadata.get("created_at", "9999"),
        )
    )

    last_runs = await _get_last_runs_by_issue(project_dir)

    queue = []
    for t in actionable:
        priority = _STATE_PRIORITY.get(t.state, 99)
        queue.append(
            {
                "issue": t.id,
                "title": t.title,
                "state": t.state.value,
                "priority": priority,
                "priority_label": f"P{priority}",
                "action": _RECOMMENDED_ACTION.get(t.state, "triage"),
                "labels": t.labels,
                "url": t.url,
                "last_run": last_runs.get(t.id),
                "created_at": t.metadata.get("created_at", ""),
            }
        )

    return queue


async def _get_last_runs_by_issue(project_dir: Path | None) -> dict[str, dict]:
    """Get the most recent TaskRun for each issue number.

    Uses a subquery to find max(id) per issue, avoiding loading all rows.
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

        return {
            r.issue_number: {
                "id": r.id,
                "status": r.status,
                "role": r.role,
                "ended_at": iso_utc(r.ended_at),
                "started_at": iso_utc(r.started_at),
            }
            for r in runs
        }
    except Exception:
        log.debug("Failed to fetch last runs for queue enrichment", exc_info=True)
        return {}

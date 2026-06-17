"""Queue service -- priority-sorted open issues from the task adapter."""

from __future__ import annotations

import re
from pathlib import Path

from sova.adapters.base import TaskState
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

# Recommended action per state
_RECOMMENDED_ACTION: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "research",
    TaskState.RESEARCHED: "develop",
    TaskState.IN_PROGRESS: "resume",
    TaskState.IN_REVIEW: "review",
    TaskState.NEEDS_SPEC: "spec",
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

    if cfg.task_source.type == "github" and not cfg.github_repo:
        return []

    try:
        adapter = create_adapter(cfg)
        tasks = await adapter.list_tasks()
    except Exception as e:
        log.warning("Failed to fetch tasks for queue: %s", e)
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
                "labels": t.labels,
                "url": t.url,
                "last_run": last_runs.get(t.id),
                "created_at": t.metadata.get("created_at", ""),
                "assignees": t.assignees,
                "issue_type": t.metadata.get("issue_type", ""),
                "jira_status": t.metadata.get("status", ""),
                "jira_priority": t.metadata.get("jira_priority", ""),
                "jira_key": t.metadata.get("key", ""),
            }
        )

    return queue


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
    except Exception:
        log.debug("Failed to fetch last runs for queue enrichment", exc_info=True)
        return {}

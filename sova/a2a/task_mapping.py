"""Bidirectional mapping between A2A task lifecycle and SOVA TaskStatus."""

from __future__ import annotations

from typing import Any

from sova.core.state import TaskStatus
from sova.db.models import TaskRun

_SOVA_TO_A2A: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "submitted",
    TaskStatus.PAUSED: "submitted",
    TaskStatus.AWAITING_APPROVAL: "submitted",
    TaskStatus.RUNNING: "working",
    TaskStatus.ASSESSING: "working",
    TaskStatus.RESEARCHED: "working",
    TaskStatus.IN_PROGRESS: "working",
    TaskStatus.DEVELOPING: "working",
    TaskStatus.SIMPLIFYING: "working",
    TaskStatus.REVIEWING: "working",
    TaskStatus.COMMITTING: "working",
    TaskStatus.PUSHING: "working",
    TaskStatus.PR_CREATED: "working",
    TaskStatus.CI_MONITORING: "working",
    TaskStatus.AUTOMATED_REVIEW: "working",
    TaskStatus.ADDRESSING_REVIEW: "working",
    TaskStatus.DONE: "completed",
    TaskStatus.FAILED: "failed",
    TaskStatus.REJECTED: "failed",
}

_A2A_TO_SOVA: dict[str, TaskStatus] = {
    "submitted": TaskStatus.PENDING,
    "working": TaskStatus.IN_PROGRESS,
    "completed": TaskStatus.DONE,
    "failed": TaskStatus.FAILED,
    "canceled": TaskStatus.REJECTED,
}


def sova_status_to_a2a(status: str | TaskStatus) -> str:
    """Map a SOVA TaskStatus to the corresponding A2A task state.

    Handles non-enum status strings (e.g. "interrupted") that are set directly
    on TaskRun records by the dashboard recovery system.
    """
    if isinstance(status, str):
        try:
            status = TaskStatus(status)
        except ValueError:
            return "failed" if status in ("interrupted",) else "working"
    return _SOVA_TO_A2A[status]


def a2a_to_sova_status(a2a_state: str) -> TaskStatus:
    """Map an A2A task state to the closest SOVA TaskStatus.

    Raises ValueError for unknown A2A states.
    """
    result = _A2A_TO_SOVA.get(a2a_state)
    if result is None:
        raise ValueError(f"Unknown A2A task state: {a2a_state!r}")
    return result


def task_run_to_a2a_task(run: TaskRun) -> dict[str, Any]:
    """Convert a SOVA TaskRun to an A2A task representation."""
    a2a_state = sova_status_to_a2a(run.status)

    task: dict[str, Any] = {
        "id": f"sova-run-{run.id}",
        "status": {
            "state": a2a_state,
            "message": run.current_step or "",
        },
        "metadata": {
            "issue_number": run.issue_number,
            "role": run.role,
            "sova_status": run.status,
            "branch_name": run.branch_name or "",
        },
        "artifacts": [],
    }

    if run.pr_number:
        task["metadata"]["pr_number"] = run.pr_number

    if run.started_at:
        task["metadata"]["started_at"] = run.started_at.isoformat()
    if run.ended_at:
        task["metadata"]["ended_at"] = run.ended_at.isoformat()

    if run.handoff_json:
        task["artifacts"].append(
            {
                "name": "handoff",
                "parts": [{"type": "data", "data": run.handoff_json}],
            }
        )

    return task

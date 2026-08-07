"""Supervisor dashboard service: queries decision logs and manages the approval plan."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from sova.db.models import SupervisorDecision
from sova.db.session import get_session
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.supervisor.progression import ProgressionDecision

log = get_logger(component="dashboard.service.supervisor")

# In-memory plan store. Ephemeral: cleared on server restart. The daemon
# rebuilds the plan on the next poll cycle so nothing is permanently lost.
_pending_plan: list["ProgressionDecision"] = []
_plan_reasoning: str | None = None
_plan_deferred: list[dict] = []


def get_pending_plan() -> list["ProgressionDecision"]:
    """Return the current pending approval plan (may be empty)."""
    return list(_pending_plan)


def get_plan_reasoning() -> str | None:
    """Return the LLM planner's reasoning text, if any."""
    return _plan_reasoning


def get_plan_deferred() -> list[dict]:
    """Return the LLM planner's deferred action list, if any."""
    return list(_plan_deferred)


def set_pending_plan(
    decisions: list["ProgressionDecision"],
    *,
    reasoning: str | None = None,
    deferred: list[dict] | None = None,
) -> None:
    """Replace the pending plan with a new set of decisions."""
    global _pending_plan, _plan_reasoning, _plan_deferred  # noqa: PLW0603
    _pending_plan = list(decisions)
    _plan_reasoning = reasoning
    _plan_deferred = list(deferred) if deferred else []


def remove_plan_items(issue_numbers: set[int]) -> list["ProgressionDecision"]:
    """Remove and return decisions for the given issue numbers from the plan.

    Decisions not in *issue_numbers* remain in the plan.
    """
    global _pending_plan
    removed = [d for d in _pending_plan if d.issue_number in issue_numbers]
    _pending_plan = [d for d in _pending_plan if d.issue_number not in issue_numbers]
    return removed


async def get_recent_decisions(
    project_dir: Path,
    *,
    project_slug: str | None = None,
    limit: int = 100,
    component: str | None = None,
    event_type: str | None = None,
) -> list[dict]:
    """Return recent supervisor decisions, newest first."""
    async with await get_session(project_dir=project_dir) as session:
        stmt = select(SupervisorDecision)
        if project_slug:
            stmt = stmt.where(SupervisorDecision.project_slug == project_slug)
        if component:
            stmt = stmt.where(SupervisorDecision.component == component)
        if event_type:
            stmt = stmt.where(SupervisorDecision.event_type == event_type)
        stmt = stmt.order_by(SupervisorDecision.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "id": row.id,
            "component": row.component,
            "event_type": row.event_type,
            "issue_number": row.issue_number,
            "action": row.action,
            "detail": row.detail,
            "metadata": row.metadata_json,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def get_decision_counts(project_dir: Path, *, project_slug: str | None = None) -> dict:
    """Return component-level decision counts for the status panel."""
    async with await get_session(project_dir=project_dir) as session:
        stmt = select(SupervisorDecision.component, func.count(SupervisorDecision.id))
        if project_slug:
            stmt = stmt.where(SupervisorDecision.project_slug == project_slug)
        stmt = stmt.group_by(SupervisorDecision.component)
        result = await session.execute(stmt)
        return dict(result.all())

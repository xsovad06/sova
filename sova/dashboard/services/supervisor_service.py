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
# Project-keyed to isolate plans in multi-project mode.
_pending_plan: dict[str, list["ProgressionDecision"]] = {}
_plan_reasoning: dict[str, str | None] = {}
_plan_deferred: dict[str, list[dict]] = {}


def resolve_project_slug(github_repo: str, project_dir: Path | None = None) -> str:
    """Derive a stable plan-store key from config.

    Uses ``github_repo`` when set.  Falls back to the project directory name
    so two projects without a repo slug never collide on the same key.
    """
    if github_repo:
        return github_repo
    if project_dir is not None:
        return f"local:{project_dir.name}"
    return ""


def get_pending_plan(project_slug: str) -> list["ProgressionDecision"]:
    """Return the current pending approval plan for a project (may be empty)."""
    return list(_pending_plan.get(project_slug, []))


def get_plan_reasoning(project_slug: str) -> str | None:
    """Return the LLM planner's reasoning text for a project, if any."""
    return _plan_reasoning.get(project_slug)


def get_plan_deferred(project_slug: str) -> list[dict]:
    """Return the LLM planner's deferred action list for a project, if any."""
    return list(_plan_deferred.get(project_slug, []))


def set_pending_plan(
    decisions: list["ProgressionDecision"],
    *,
    project_slug: str,
    reasoning: str | None = None,
    deferred: list[dict] | None = None,
) -> None:
    """Replace the pending plan for a project with a new set of decisions."""
    global _pending_plan, _plan_reasoning, _plan_deferred  # noqa: PLW0603
    _pending_plan[project_slug] = list(decisions)
    _plan_reasoning[project_slug] = reasoning
    _plan_deferred[project_slug] = list(deferred) if deferred else []


def remove_plan_items(issue_numbers: set[int], project_slug: str) -> list["ProgressionDecision"]:
    """Remove and return decisions for the given issue numbers from a project's plan.

    Decisions not in *issue_numbers* remain in the plan.
    """
    global _pending_plan
    plan = _pending_plan.get(project_slug, [])
    if not plan:
        return []
    removed = [d for d in plan if d.issue_number in issue_numbers]
    if removed:
        _pending_plan[project_slug] = [d for d in plan if d.issue_number not in issue_numbers]
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

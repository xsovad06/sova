"""Supervisor dashboard service: queries decision logs for the activity stream."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from sova.db.models import SupervisorDecision
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.service.supervisor")


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

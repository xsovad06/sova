"""Task service -- active tasks and task history from the DB."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun

# Terminal statuses -- must match sova/core/state.py and run_service.py
_TERMINAL = frozenset({"done", "failed", "rejected"})


async def get_active_tasks(session: AsyncSession) -> list[dict]:
    """Get non-terminal task runs as active task cards."""
    stmt = select(TaskRun).where(TaskRun.status.notin_(_TERMINAL)).order_by(TaskRun.started_at.desc())
    result = await session.execute(stmt)
    runs = result.scalars().all()

    now = datetime.now(timezone.utc)
    tasks = []
    for r in runs:
        started = r.started_at or now
        # SQLite returns naive datetimes; ensure both are aware or both naive
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        time_in_state = _format_duration(now - started)
        tasks.append(
            {
                "id": r.id,
                "issue_number": r.issue_number,
                "role": r.role,
                "status": r.status,
                "current_step": r.current_step,
                "branch_name": r.branch_name,
                "pr_number": r.pr_number,
                "time_in_state": time_in_state,
                "started_at": started.isoformat() if r.started_at else None,
                "total_cost_usd": float(r.total_cost_usd or 0),
            }
        )
    return tasks


async def get_task_history(session: AsyncSession, limit: int = 50) -> list[dict]:
    """Get completed/failed task runs."""
    stmt = select(TaskRun).where(TaskRun.status.in_(_TERMINAL)).order_by(TaskRun.ended_at.desc()).limit(limit)
    result = await session.execute(stmt)
    runs = result.scalars().all()

    return [
        {
            "id": r.id,
            "issue_number": r.issue_number,
            "role": r.role,
            "status": r.status,
            "current_step": r.current_step,
            "total_cost_usd": float(r.total_cost_usd or 0),
            "error_message": r.error_message,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        }
        for r in runs
    ]


def _format_duration(td) -> str:
    """Format a timedelta as human-readable string."""
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    hours = total // 3600
    mins = (total % 3600) // 60
    return f"{hours}h {mins}m"

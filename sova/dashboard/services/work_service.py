"""Work service -- unified view of active tasks and run history.

Merges task_service + run_service into a single coherent service
for the Work page (Active / History / Failed tabs).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.control_service import DEVELOPER_PIPELINE, get_step_progress
from sova.db.models import StepExecution, TaskRun
from sova.utils.formatting import iso_utc

_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted"})


async def get_active_work(session: AsyncSession) -> list[dict]:
    """Get non-terminal task runs with step progress info."""
    stmt = select(TaskRun).where(TaskRun.status.notin_(_TERMINAL)).order_by(TaskRun.started_at.desc())
    result = await session.execute(stmt)
    runs = result.scalars().all()

    now = datetime.now(timezone.utc)
    items = []
    for r in runs:
        started = r.started_at or now
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        elapsed = now - started
        progress = get_step_progress(r.current_step)

        items.append(
            {
                "id": r.id,
                "issue_number": r.issue_number,
                "role": r.role,
                "status": r.status,
                "current_step": r.current_step,
                "step_index": progress["step_index"],
                "total_steps": progress["total_steps"],
                "branch_name": r.branch_name,
                "pr_number": r.pr_number,
                "elapsed_seconds": int(elapsed.total_seconds()),
                "elapsed_formatted": _format_duration(elapsed),
                "started_at": iso_utc(started),
                "total_cost_usd": float(r.total_cost_usd or 0),
            }
        )
    return items


async def get_work_history(
    session: AsyncSession,
    *,
    status: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get completed/failed/interrupted task runs with step counts."""
    stmt = select(TaskRun).where(TaskRun.status.in_(_TERMINAL)).order_by(TaskRun.ended_at.desc())

    if status:
        stmt = stmt.where(TaskRun.status == status)
    if role:
        stmt = stmt.where(TaskRun.role == role)
    stmt = stmt.limit(min(limit, 200))

    result = await session.execute(stmt)
    runs = result.scalars().all()

    items = []
    for r in runs:
        step_count = await session.scalar(select(func.count(StepExecution.id)).where(StepExecution.task_run_id == r.id))
        completed_steps = await session.scalar(
            select(func.count(StepExecution.id)).where(
                StepExecution.task_run_id == r.id,
                StepExecution.status == "done",
            )
        )

        duration_ms = None
        if r.started_at and r.ended_at:
            started = r.started_at
            ended = r.ended_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            duration_ms = int((ended - started).total_seconds() * 1000)

        items.append(
            {
                "id": r.id,
                "issue_number": r.issue_number,
                "role": r.role,
                "status": r.status,
                "current_step": r.current_step,
                "steps_completed": completed_steps or 0,
                "steps_total": step_count or 0,
                "total_steps_possible": len(DEVELOPER_PIPELINE),
                "branch_name": r.branch_name,
                "pr_number": r.pr_number,
                "total_cost_usd": float(r.total_cost_usd or 0),
                "duration_ms": duration_ms,
                "duration_formatted": _format_duration_ms(duration_ms) if duration_ms else None,
                "error_message": r.error_message,
                "started_at": iso_utc(r.started_at),
                "ended_at": iso_utc(r.ended_at),
            }
        )
    return items


async def get_work_detail(session: AsyncSession, run_id: int) -> dict | None:
    """Get a single run with its step executions and pipeline progress."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None

    steps_stmt = select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.started_at)
    steps_result = await session.execute(steps_stmt)
    steps = steps_result.scalars().all()

    progress = get_step_progress(run.current_step)

    return {
        "run": _run_to_dict(run),
        "steps": [_step_to_dict(s) for s in steps],
        "pipeline": progress,
    }


async def get_work_summary(session: AsyncSession) -> dict:
    """Aggregate counts for overview cards."""
    total = await session.scalar(select(func.count(TaskRun.id))) or 0
    done = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "done")) or 0
    failed = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "failed")) or 0
    active = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status.not_in(_TERMINAL))) or 0

    return {"total": total, "done": done, "failed": failed, "active": active}


def _run_to_dict(run: TaskRun) -> dict:
    return {
        "id": run.id,
        "issue_number": run.issue_number,
        "role": run.role,
        "status": run.status,
        "current_step": run.current_step,
        "branch_name": run.branch_name,
        "pr_number": run.pr_number,
        "total_cost_usd": float(run.total_cost_usd or 0),
        "error_message": run.error_message,
        "project_slug": run.project_slug,
        "resumed_from_id": run.resumed_from_id,
        "started_at": iso_utc(run.started_at),
        "ended_at": iso_utc(run.ended_at),
    }


def _step_to_dict(step: StepExecution) -> dict:
    return {
        "id": step.id,
        "step_name": step.step_name,
        "status": step.status,
        "cost_usd": float(step.cost_usd),
        "duration_ms": step.duration_ms,
        "duration_formatted": _format_duration_ms(step.duration_ms) if step.duration_ms else None,
        "output_summary": step.output_summary,
        "gate_check_result": step.gate_check_result,
        "error_message": step.error_message,
        "started_at": iso_utc(step.started_at),
        "ended_at": iso_utc(step.ended_at),
    }


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


def _format_duration_ms(ms: int | None) -> str | None:
    """Format milliseconds as human-readable string."""
    if ms is None:
        return None
    total = ms // 1000
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    hours = total // 3600
    mins = (total % 3600) // 60
    return f"{hours}h {mins}m"

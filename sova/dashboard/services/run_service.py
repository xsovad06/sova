"""Run history queries -- TaskRun + StepExecution from the database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import StepExecution, TaskRun

_TERMINAL_STATUSES = {"done", "failed", "rejected"}


async def list_runs(
    session: AsyncSession,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict]:
    """List task runs, most recent first."""
    stmt = select(TaskRun).order_by(TaskRun.started_at.desc())
    if status:
        stmt = stmt.where(TaskRun.status == status)
    stmt = stmt.limit(min(limit, 200))

    result = await session.execute(stmt)
    return [_run_to_dict(r) for r in result.scalars().all()]


async def get_run(session: AsyncSession, run_id: int) -> dict | None:
    """Get a single task run by ID."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None
    return _run_to_dict(run)


async def get_run_steps(session: AsyncSession, run_id: int) -> list[dict]:
    """Get all step executions for a run, in order."""
    stmt = select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.started_at)
    result = await session.execute(stmt)
    return [_step_to_dict(s) for s in result.scalars().all()]


async def get_run_summary(session: AsyncSession) -> dict:
    """Aggregate counts for overview."""
    total = await session.scalar(select(func.count(TaskRun.id)))
    done = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "done"))
    failed = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "failed"))

    # "active" means any status that isn't terminal
    terminal = _TERMINAL_STATUSES
    active = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status.not_in(terminal)))

    return {
        "total": total or 0,
        "done": done or 0,
        "failed": failed or 0,
        "active": active or 0,
    }


async def mark_run_failed(session: AsyncSession, run_id: int, reason: str = "Manually abandoned") -> dict | None:
    """Mark a non-terminal run as failed (e.g. stale paused runs)."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None
    terminal = _TERMINAL_STATUSES
    if run.status in terminal:
        return {"error": f"Run is already {run.status}"}
    run.status = "failed"
    run.error_message = reason
    if not run.ended_at:
        run.ended_at = datetime.now(timezone.utc)
    await session.flush()
    return _run_to_dict(run)


def _run_to_dict(run: TaskRun) -> dict:
    return {
        "id": run.id,
        "issue_number": run.issue_number,
        "role": run.role,
        "status": run.status,
        "current_step": run.current_step,
        "branch_name": run.branch_name,
        "pr_number": run.pr_number,
        "total_cost_usd": float(run.total_cost_usd),
        "error_message": run.error_message,
        "project_slug": run.project_slug,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


def _step_to_dict(step: StepExecution) -> dict:
    return {
        "id": step.id,
        "step_name": step.step_name,
        "status": step.status,
        "cost_usd": float(step.cost_usd),
        "duration_ms": step.duration_ms,
        "output_summary": step.output_summary,
        "gate_check_result": step.gate_check_result,
        "error_message": step.error_message,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "ended_at": step.ended_at.isoformat() if step.ended_at else None,
    }

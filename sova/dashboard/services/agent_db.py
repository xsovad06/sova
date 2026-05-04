"""Agent DB persistence -- TaskRun/CostRecord CRUD for dashboard-spawned agents.

Separated from agent_lifecycle to keep DB logic focused and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sova.dashboard.services.agent_pool import AgentState
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.agent_db")


async def _create_task_run(
    issue: str, role: str, project_dir: Path, *, pid: int | None = None, pr_number: int | None = None
) -> int | None:
    """Create a TaskRun record and return its ID."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number=issue,
                    role=role,
                    status="running",
                    current_step="agent",
                    pid=pid,
                    pr_number=pr_number,
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id
        log.info("task_run.created", run_id=run_id, issue=issue)
        return run_id
    except Exception:
        log.warning("task_run.create_failed", exc_info=True)
        return None


async def _set_output_file_path(run_id: int, path: Path, project_dir: Path) -> None:
    """Store the output file path on the TaskRun record."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.output_file_path = str(path)
    except Exception:
        log.debug("output_file_path.set_failed", run_id=run_id, exc_info=True)


async def _finalize_task_run(run_id: int, *, exit_code: int, agent: AgentState) -> None:
    """Update the TaskRun with final status and cost."""
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(agent.last_result_cost)) if agent.last_result_cost else Decimal("0")

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return
                task_run.status = status
                task_run.total_cost_usd = cost
                task_run.ended_at = datetime.now(timezone.utc)
                if exit_code != 0:
                    task_run.error_message = f"Process exited with code {exit_code}"

                if agent.last_result_cost and agent.last_result_cost > 0:
                    cost_record = CostRecord(
                        task_run_id=run_id,
                        phase="agent",
                        issue=task_run.issue_number,
                        model="claude",
                        cost_usd=cost,
                    )
                    session.add(cost_record)
        log.info("task_run.finalized", run_id=run_id, status=status, cost=float(cost))
    except Exception:
        log.warning("task_run.finalize_failed", exc_info=True)


async def _fetch_run_states(run_ids: list[int]) -> dict[int, dict]:
    """Fetch current_step, status, and cost from the DB for running agents."""
    if not run_ids:
        return {}
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.id.in_(run_ids))
                result = await session.execute(stmt)
                runs = result.scalars().all()
        return {
            r.id: {
                "current_step": r.current_step or "agent",
                "status": r.status,
                "cost_usd": float(r.total_cost_usd or 0),
                "pr_number": r.pr_number,
            }
            for r in runs
        }
    except Exception:
        log.debug("fetch_run_states.failed", exc_info=True)
        return {}

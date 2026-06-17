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
    issue: str | None, role: str, project_dir: Path, *, pid: int | None = None, pr_number: int | None = None
) -> int | None:
    """Create a TaskRun record and return its ID."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number=issue or None,
                    role=role,
                    status="running",
                    current_step="agent",
                    pid=pid,
                    pr_number=pr_number,
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id
        log.info("task_run.created", run_id=run_id, issue=issue or "(none)")
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


async def _update_task_run_pid(run_id: int, pid: int, project_dir: Path) -> None:
    """Set the PID on an existing TaskRun after process spawn."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.pid = pid
    except Exception:
        log.warning("task_run.pid_update_failed", run_id=run_id, exc_info=True)


async def _finalize_orphaned_run(run_id: int, project_dir: Path) -> None:
    """Mark a TaskRun as failed when the process never started."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.status = "failed"
                    task_run.error_message = "Process spawn failed"
                    task_run.ended_at = datetime.now(timezone.utc)
    except Exception:
        log.warning("task_run.orphan_cleanup_failed", run_id=run_id, exc_info=True)


_TERMINAL_STATUSES = frozenset({"done", "failed", "rejected", "interrupted", "paused"})


def _read_file_handoff(project_dir: Path, issue: str = "") -> dict | None:
    """Read file-based handoff details (sync I/O, call outside async transactions)."""
    try:
        from sova.ipc.handoff import read_handoff_file

        handoff = read_handoff_file(project_dir, issue=issue or None)
        if handoff is None:
            return None
        return {
            "issue": handoff.issue,
            "pr_number": handoff.pr_number,
            "details": handoff.details,
            "source": handoff.source,
        }
    except Exception:
        log.debug("task_run.file_handoff_read_failed", exc_info=True)
        return None


def _apply_file_handoff(task_run: object, file_handoff: dict | None, run_id: int) -> None:
    """Apply file-based handoff to a TaskRun if it matches by issue or PR."""
    if not file_handoff:
        return
    handoff_issue = str(file_handoff["issue"]).lstrip("#").strip() if file_handoff["issue"] else ""
    run_issue = str(task_run.issue_number).lstrip("#").strip() if task_run.issue_number else ""
    issue_match = (handoff_issue and run_issue and handoff_issue == run_issue) or (not handoff_issue and not run_issue)
    pr_match = file_handoff["pr_number"] and file_handoff["pr_number"] == task_run.pr_number
    if issue_match or pr_match:
        task_run.handoff_json = file_handoff["details"]
        log.info("task_run.file_handoff_persisted", run_id=run_id, source=file_handoff["source"])


async def _finalize_task_run(run_id: int, *, exit_code: int, agent: AgentState) -> None:
    """Update the TaskRun with final status and cost.

    Status is only updated if not already terminal (the WorkflowEngine may
    have set it first). Cost is always updated from the stream output since
    it includes Claude Code's own overhead.
    """
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(agent.last_result_cost)) if agent.last_result_cost else Decimal("0")
        file_handoff = _read_file_handoff(agent.project_dir, issue=agent.issue)

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return

                if cost > 0:
                    task_run.total_cost_usd = cost

                if task_run.status in _TERMINAL_STATUSES:
                    log.info("task_run.already_terminal", run_id=run_id, status=task_run.status)
                    return

                task_run.status = status
                task_run.ended_at = datetime.now(timezone.utc)
                if exit_code != 0:
                    task_run.error_message = f"Process exited with code {exit_code}"

                if agent.last_result_cost and agent.last_result_cost > 0:
                    cost_record = CostRecord(
                        task_run_id=run_id,
                        phase="agent",
                        issue=task_run.issue_number or task_run.run_label or "",
                        model="claude",
                        cost_usd=cost,
                    )
                    session.add(cost_record)

                if not task_run.handoff_json:
                    _apply_file_handoff(task_run, file_handoff, run_id)

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

"""Already-running gate: blocks when an agent is active for the same issue."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import TaskRun
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger
from sova.utils.process import is_process_alive

log = get_logger(component="supervisor.gates.already_running")

_ALREADY_RUNNING_TERMINAL = TASK_RUN_TERMINAL - {"awaiting_approval"}


async def check_already_running(issue: int, session_factory: async_sessionmaker) -> BlockReason | None:
    """Check if an agent is already running or being started for this issue.

    awaiting_approval runs (spec pending human review) block unconditionally: the researcher
    completed its work and the issue should not be re-researched even though the process exited.
    """
    try:
        async with session_factory() as session:
            stmt = select(TaskRun).where(
                TaskRun.issue_number == str(issue),
                TaskRun.status.notin_(_ALREADY_RUNNING_TERMINAL),
            )
            result = await session.execute(stmt)
            runs = result.scalars().all()
            for run in runs:
                if run.status == "awaiting_approval":
                    return BlockReason(
                        gate="already_running",
                        detail=f"Spec awaiting human approval for #{issue} (run {run.id})",
                    )
                if run.pid is None:
                    return BlockReason(
                        gate="already_running",
                        detail=f"Agent being started for #{issue} (run {run.id}, PID not yet assigned)",
                    )
                if is_process_alive(run.pid):
                    return BlockReason(
                        gate="already_running",
                        detail=f"Agent already running for #{issue} (run {run.id}, PID {run.pid})",
                    )
    except Exception:
        log.debug("already_running.check_failed", issue=issue, exc_info=True)

    return None

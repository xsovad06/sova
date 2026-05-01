"""Stale run detection and PID liveness checks."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control.recovery")


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale 'running' TaskRuns on dashboard startup."""
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        interrupted = []

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.status == "running")
                result = await session.execute(stmt)
                stale_runs = result.scalars().all()

                for run in stale_runs:
                    if run.pid and _is_process_alive(run.pid):
                        log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                        continue

                    run.status = "interrupted"
                    run.error_message = "Dashboard restarted while agent was running"
                    run.ended_at = datetime.now(timezone.utc)
                    interrupted.append(
                        {
                            "run_id": run.id,
                            "issue": run.issue_number,
                            "role": run.role,
                            "pid": run.pid,
                        }
                    )
                    log.warning("recovery.interrupted", run_id=run.id, issue=run.issue_number, pid=run.pid)

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))
        return interrupted
    except Exception:
        log.warning("recovery.failed", exc_info=True)
        return []

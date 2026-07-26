"""Non-blocking telemetry push to a remote hub after pipeline finalization.

Fire-and-forget: never blocks finalization, never affects TaskRun status,
swallows all exceptions at DEBUG level.
"""

from __future__ import annotations

import getpass
import hashlib
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="telemetry.push")


def _derive_machine_id(cfg_machine_id: str) -> str:
    """Return configured machine_id or auto-derive from hostname+username."""
    if cfg_machine_id:
        return cfg_machine_id
    raw = f"{socket.gethostname()}:{getpass.getuser()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def push_telemetry(run_id: int, project_dir: Path, cfg: ProjectConfig) -> None:
    """Push a run summary to the configured hub. Fire-and-forget.

    Catches all exceptions (except CancelledError) and logs at DEBUG.
    """
    try:
        import httpx

        from sova.db.models import StepExecution, TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                log.debug("push_telemetry.no_run", run_id=run_id)
                return

            from sqlalchemy import select

            stmt = select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.started_at)
            result = await session.execute(stmt)
            steps = result.scalars().all()

            # Build payload
            machine_id = _derive_machine_id(cfg.telemetry.machine_id)
            project_slug = task_run.project_slug or project_dir.name

            exit_step: str | None = None
            for se in steps:
                if se.status == "failed":
                    exit_step = se.step_name
                    break

            duration_seconds: float | None = None
            if task_run.started_at and task_run.ended_at:
                duration_seconds = (task_run.ended_at - task_run.started_at).total_seconds()

            step_outcomes: dict[str, str] = {se.step_name: se.status for se in steps}

            payload: dict[str, Any] = {
                "machine_id": machine_id,
                "project_slug": project_slug,
                "run_id": str(run_id),
                "role": task_run.role,
                "status": task_run.status,
                "exit_step": exit_step,
                "failure_message": task_run.error_message[:500] if task_run.error_message else None,
                "cost_usd": str(task_run.total_cost_usd or 0),
                "duration_seconds": duration_seconds,
                "step_outcomes": step_outcomes,
                "run_at": task_run.started_at.isoformat() if task_run.started_at else None,
            }

        # POST to hub
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.telemetry.hub_token:
            headers["Authorization"] = f"Bearer {cfg.telemetry.hub_token}"

        hub_url = cfg.telemetry.hub_url.rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{hub_url}/api/telemetry/ingest", json=payload, headers=headers)
            resp.raise_for_status()
            log.debug("push_telemetry.sent", run_id=run_id, status_code=resp.status_code)

    except Exception:
        log.debug("push_telemetry.failed", run_id=run_id, exc_info=True)

"""Supervisor API router: status, manual poll trigger, decision log queries."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.project_context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.supervisor")

router = APIRouter(prefix="/supervisor", tags=["supervisor"])

_daemon_registry: dict = {}
_background_tasks: set[asyncio.Task] = set()


def _get_daemon() -> Any | None:
    """Get the daemon instance for the current project, if any."""
    project_dir = get_project_dir()
    if project_dir is None:
        return None
    return _daemon_registry.get(str(project_dir.resolve()))


def set_daemon_registry(registry: dict) -> None:
    """Called by app.py lifespan to share the daemon registry."""
    global _daemon_registry
    _daemon_registry = registry


@router.get("/status")
async def get_status():
    """Return supervisor daemon status."""
    daemon = _get_daemon()
    if daemon is None:
        return {"enabled": False, "running": False}
    return await daemon.get_status()


@router.post("/poll", status_code=202)
async def trigger_poll():
    """Trigger a manual poll cycle (fire-and-forget)."""
    daemon = _get_daemon()
    if daemon is None:
        raise HTTPException(status_code=404, detail="Supervisor daemon is not running")
    task = asyncio.create_task(daemon.poll_once())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "accepted"}


@router.get("/decisions")
async def get_decisions(
    limit: int = Query(100, ge=1, le=1000),
    component: str | None = None,
    event_type: str | None = None,
):
    """Return recent supervisor decisions."""
    from sova.dashboard.services.supervisor_service import get_recent_decisions

    project_dir = get_project_dir()
    decisions = await get_recent_decisions(
        project_dir,
        limit=limit,
        component=component,
        event_type=event_type,
    )
    return {"decisions": decisions}


@router.get("/counts")
async def get_counts():
    """Return per-component decision counts."""
    from sova.dashboard.services.supervisor_service import get_decision_counts

    project_dir = get_project_dir()
    counts = await get_decision_counts(project_dir)
    return {"counts": counts}

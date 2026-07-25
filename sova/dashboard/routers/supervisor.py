"""Supervisor API router: status, manual poll trigger, decision log queries."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.project_context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.supervisor")

router = APIRouter(prefix="/supervisor", tags=["supervisor"])

_daemon_registry: dict = {}
_background_tasks: set[asyncio.Task] = set()
_start_lock = asyncio.Lock()


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
    return daemon.get_status()


@router.post("/poll", status_code=202, responses={404: {"description": "Supervisor daemon is not running"}})
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
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    component: str | None = None,
    event_type: str | None = None,
):
    """Return recent supervisor decisions."""
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import get_recent_decisions

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
        project_slug = cfg.github_repo or None
    except Exception:
        project_slug = None
    decisions = await get_recent_decisions(
        project_dir,
        project_slug=project_slug,
        limit=limit,
        component=component,
        event_type=event_type,
    )
    return {"decisions": decisions}


@router.post("/start", responses={409: {"description": "supervisor.enabled is false in config"}})
async def start_supervisor() -> dict[str, Any]:
    """Start the supervisor daemon for the current project.

    Safe to call after enabling supervisor.enabled in settings without a server restart.
    Returns the daemon status after starting (or the existing status if already running).
    """
    from sova.config.loader import load_config
    from sova.db.session import get_session_factory
    from sova.supervisor.daemon import SupervisorDaemon

    project_dir = get_project_dir()
    if project_dir is None:
        raise HTTPException(status_code=503, detail="No project context")

    async with _start_lock:
        daemon = _get_daemon()
        if daemon is not None and daemon.running:
            return {"started": False, "reason": "already running", **daemon.get_status()}

        cfg = load_config(project_dir)
        if not cfg.supervisor.enabled:
            raise HTTPException(status_code=409, detail="supervisor.enabled is false in config — enable it first")

        session_factory = await get_session_factory(project_dir)
        new_daemon = SupervisorDaemon(config=cfg, project_dir=project_dir, session_factory=session_factory)
        new_daemon.start()
        _daemon_registry[str(project_dir.resolve())] = new_daemon
        log.info("supervisor.started_via_api", project_dir=str(project_dir))
        return {"started": True, **new_daemon.get_status()}


@router.post("/stop", responses={404: {"description": "Supervisor daemon is not running"}})
async def stop_supervisor() -> dict[str, Any]:
    """Stop the supervisor daemon for the current project.

    Cancels the polling loop and removes the daemon from the registry.
    Config is not modified; re-enable via POST /supervisor/start.
    """
    project_dir = get_project_dir()
    if project_dir is None:
        raise HTTPException(status_code=503, detail="No project context")

    async with _start_lock:
        daemon = _get_daemon()
        if daemon is None or not daemon.running:
            raise HTTPException(status_code=404, detail="Supervisor daemon is not running")
        await daemon.stop()
        _daemon_registry.pop(str(project_dir.resolve()), None)
        log.info("supervisor.stopped_via_api", project_dir=str(project_dir))
        return {"stopped": True, "running": False}


@router.get("/counts")
async def get_counts():
    """Return per-component decision counts."""
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import get_decision_counts

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
        project_slug = cfg.github_repo or None
    except Exception:
        project_slug = None
    counts = await get_decision_counts(project_dir, project_slug=project_slug)
    return {"counts": counts}

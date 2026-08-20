"""A2A protocol API router -- Agent Card discovery and task management."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from sova.a2a.agent_card import generate_role_card
from sova.a2a.task_mapping import sova_status_to_a2a, task_run_to_a2a_task
from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.config.models import ProjectConfig
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.db.models import TaskRun
from sova.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(tags=["a2a"])

_CONFIG_CACHE_TTL = 60.0
_cached_config = None
_cached_config_dir = None
_cached_config_ts = 0.0


def _get_a2a_config() -> ProjectConfig | None:
    """Load A2A config with a TTL cache to avoid disk reads on every request."""
    global _cached_config, _cached_config_dir, _cached_config_ts

    project_dir = get_project_dir() or get_default_project_dir()
    if project_dir is None:
        return None

    now = time.monotonic()
    if (
        _cached_config is not None
        and _cached_config_dir == project_dir
        and (now - _cached_config_ts) < _CONFIG_CACHE_TTL
    ):
        return _cached_config

    _cached_config = load_config(project_dir)
    _cached_config_dir = project_dir
    _cached_config_ts = now
    return _cached_config


def _require_a2a_enabled() -> None:
    """Raise 404 if A2A protocol is disabled in config."""
    cfg = _get_a2a_config()
    if cfg is None or not cfg.a2a.enabled:
        raise HTTPException(status_code=404, detail="A2A protocol is disabled")


def _parse_run_id(task_id: str) -> int | None:
    """Extract the numeric TaskRun ID from an A2A task ID like 'sova-run-42'."""
    prefix = "sova-run-"
    if task_id.startswith(prefix):
        try:
            return int(task_id[len(prefix) :])
        except ValueError:
            return None
    return None


@router.get("/tasks")
async def list_a2a_tasks(request: Request, limit: int = 50, offset: int = 0):
    """List recent SOVA tasks in A2A format with pagination."""
    _require_a2a_enabled()

    capped_limit = min(max(limit, 1), 100)
    capped_offset = max(offset, 0)

    async with await get_session() as session, session.begin():
        count_stmt = select(func.count(TaskRun.id))
        total = (await session.execute(count_stmt)).scalar() or 0

        stmt = select(TaskRun).order_by(TaskRun.started_at.desc()).offset(capped_offset).limit(capped_limit)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        tasks = [task_run_to_a2a_task(r) for r in runs]

    return {"tasks": tasks, "total": total, "limit": capped_limit, "offset": capped_offset}


@router.get("/tasks/{task_id}")
async def get_a2a_task(request: Request, task_id: str):
    """Get a single SOVA task in A2A format."""
    _require_a2a_enabled()

    run_id = _parse_run_id(task_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Invalid task ID: {task_id}")

    async with await get_session() as session, session.begin():
        run = await session.get(TaskRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return task_run_to_a2a_task(run)


@router.post("/tasks/{task_id}/cancel")
async def cancel_a2a_task(request: Request, task_id: str):
    """Cancel a running SOVA task."""
    _require_a2a_enabled()

    run_id = _parse_run_id(task_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Invalid task ID: {task_id}")

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.dashboard.services import control_service

    async with await get_session() as session, session.begin():
        run = await session.get(TaskRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        if run.status in TASK_RUN_TERMINAL:
            a2a_state = sova_status_to_a2a(run.status)
            return {"id": task_id, "status": {"state": a2a_state, "message": "Already terminal"}}
        run.status = "rejected"

    try:
        await control_service.stop_agent(run_id=run_id)
    except Exception:
        log.warning("stop_agent failed after marking run %d rejected", run_id, exc_info=True)
    return {"id": task_id, "status": {"state": "canceled", "message": "Task canceled"}}


@router.get("/{role}/agent.json")
async def role_agent_card(request: Request, role: str):
    """Return the A2A Agent Card for a specific SOVA role."""
    _require_a2a_enabled()
    cfg = _get_a2a_config()
    endpoint_base = (cfg.a2a.endpoint_base if cfg else "") or str(request.base_url).rstrip("/")
    card = generate_role_card(role, endpoint_base=endpoint_base)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")
    return card

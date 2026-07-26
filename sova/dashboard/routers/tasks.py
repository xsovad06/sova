"""Tasks router -- active tasks and task history."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.task_service import get_active_tasks, get_task_history
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.tasks")

router = APIRouter(tags=["tasks"])

_issues_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60


@router.get("/tasks/issues")
async def list_issues():
    """Get all open issues from the task source (cached 60s)."""
    project_dir = get_project_dir()
    cache_key = str(project_dir or "default")

    cached = _issues_cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return {"issues": cached[1]}

    try:
        from sova.adapters import create_adapter
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
        tasks = await adapter.list_tasks()

        issues = [
            {
                "number": t.id,
                "title": t.title,
                "state": t.state.value,
                "labels": t.labels,
            }
            for t in tasks
        ]
        _issues_cache[cache_key] = (time.time(), issues)
        return {"issues": issues}
    except Exception as exc:
        log.warning("Failed to fetch issues from task source", exc_info=True)
        raise HTTPException(status_code=503, detail="Task source unavailable") from exc


@router.get("/tasks/active")
async def active_tasks():
    """Get currently active (non-terminal) tasks."""
    project_dir = get_project_dir()
    async with await get_session(project_dir) as session:
        tasks = await get_active_tasks(session)
    return {"tasks": tasks}


@router.get("/tasks/history")
async def task_history(limit: int = Query(default=50, ge=1, le=500)):
    """Get completed/failed task history."""
    project_dir = get_project_dir()
    async with await get_session(project_dir) as session:
        result = await get_task_history(session, limit=limit)
    return {"tasks": result["tasks"]}

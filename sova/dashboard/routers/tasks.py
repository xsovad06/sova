"""Tasks router: open issues from the task source."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.tasks")

router = APIRouter(tags=["tasks"])

_issues_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60


@router.get("/tasks/issues")
async def list_issues() -> dict:
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
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("Failed to fetch issues from task source", exc_info=True)
        raise HTTPException(status_code=503, detail="Task source unavailable") from exc

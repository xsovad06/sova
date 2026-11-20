"""Tasks router: open issues from the task source."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.dashboard.services.work_state import state_color, state_label
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.tasks")

router = APIRouter(tags=["tasks"])

_issues_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60


@router.get("/tasks/issues", responses={503: {"description": "Task source unavailable"}})
async def list_issues() -> dict:
    """Get all open issues from the task source (cached 60s)."""
    project_dir = get_project_dir()
    cache_key = str(project_dir or "default")

    cached = _issues_cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return {"issues": cached[1]}

    from sova.adapters import create_adapter

    try:
        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
        tasks = await adapter.list_tasks()
    except Exception as exc:  # noqa: BLE001 (a bad config, tracker outage or expired auth is a 503, whatever it raises)
        log.warning("tasks.issues.source_unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="Task source unavailable") from exc

    issues = [
        {
            "number": t.id,
            "title": t.title,
            "state": t.state.value,
            "state_label": state_label(t.state.value),
            "state_color": state_color(t.state.value),
            "labels": t.labels,
        }
        for t in tasks
    ]
    _issues_cache[cache_key] = (time.time(), issues)
    return {"issues": issues}

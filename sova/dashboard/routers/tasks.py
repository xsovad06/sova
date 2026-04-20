"""Tasks router -- active tasks and task history."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.task_service import get_active_tasks, get_task_history
from sova.db.session import get_session

router = APIRouter(tags=["tasks"])


@router.get("/tasks/active")
async def active_tasks():
    """Get currently active (non-terminal) tasks."""
    project_dir = get_project_dir()
    async with await get_session(project_dir) as session:
        tasks = await get_active_tasks(session)
    return {"tasks": tasks}


@router.get("/tasks/history")
async def task_history(limit: int = 50):
    """Get completed/failed task history."""
    project_dir = get_project_dir()
    async with await get_session(project_dir) as session:
        tasks = await get_task_history(session, limit=limit)
    return {"tasks": tasks}

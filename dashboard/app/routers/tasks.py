"""Tasks API — active tasks and history."""

from fastapi import APIRouter

from app import config
from app.services import task_service

router = APIRouter()


@router.get("/tasks/active")
async def active_tasks():
    tasks = task_service.get_active_tasks()
    return {"tasks": tasks, "github_repo": config.GITHUB_REPO}


@router.get("/tasks/history")
async def task_history():
    return task_service.get_task_history()


@router.get("/tasks/summary")
async def task_summary():
    return task_service.get_task_summary()

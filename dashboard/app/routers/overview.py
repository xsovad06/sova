"""Overview API — aggregated dashboard data."""

from fastapi import APIRouter

from app.services import costs_service, log_service, memory_service, task_service

router = APIRouter()


@router.get("/overview")
async def overview():
    return {
        "costs": costs_service.get_summary(),
        "tasks": task_service.get_task_summary(),
        "recent_logs": log_service.get_recent(10),
        "log_counts": log_service.get_counts(),
        "memory_count": memory_service.get_memory_count(),
        "memory_files": memory_service.list_markdown_files(),
    }

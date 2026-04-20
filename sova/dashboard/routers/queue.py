"""Queue router -- priority-sorted issue queue."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.control_service import start_agent
from sova.dashboard.services.queue_service import get_priority_queue

router = APIRouter(tags=["queue"])


class StartFromQueueRequest(BaseModel):
    role: str | None = None
    force: bool = False


@router.get("/queue")
async def queue():
    """Get the priority-sorted issue queue."""
    project_dir = get_project_dir()
    items = await get_priority_queue(project_dir)
    return {"queue": items}


@router.post("/queue/start/{issue}")
async def start_from_queue(issue: str, req: StartFromQueueRequest):
    """Start an agent run for an issue from the queue."""
    result = await start_agent(issue=issue, role=req.role, force=req.force)
    return result

"""Queue router -- priority-sorted issue queue."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import batch_service
from sova.dashboard.services.control_service import start_agent
from sova.dashboard.services.queue_service import get_priority_queue

router = APIRouter(tags=["queue"])


class StartFromQueueRequest(BaseModel):
    role: str | None = None
    force: bool = False


class BatchRequest(BaseModel):
    issues: list[str]
    action: str
    options: dict = {}


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


@router.post("/queue/batch")
async def start_batch(req: BatchRequest):
    """Start a batch action on selected issues."""
    project_dir = get_project_dir()

    if req.action == "run":
        return await batch_service.start_batch_run(req.issues, project_dir)

    if req.action not in ("triage", "harden"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    batch_id = await batch_service.start_batch(
        action=req.action,
        issue_ids=req.issues,
        project_dir=project_dir,
        options=req.options,
    )
    return {"batch_id": batch_id, "status": "started", "count": len(req.issues)}


@router.get("/queue/batch/active")
async def active_batch():
    """Return the currently running batch for the current project, if any."""
    project_dir = get_project_dir()
    active = batch_service.get_active_batch(project_dir)
    if active is None:
        return {"active": False}
    return {"active": True, "batch": active}


@router.get("/queue/batch/{batch_id}/status")
async def batch_status(batch_id: str):
    """Get progress of a batch operation."""
    status = batch_service.get_batch_status(batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return status


@router.post("/queue/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """Cancel a running batch operation."""
    cancelled = batch_service.cancel_batch(batch_id)
    return {"cancelled": cancelled}

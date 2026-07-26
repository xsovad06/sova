"""Queue router -- priority-sorted issue queue."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, field_validator

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import batch_service
from sova.dashboard.services.control_service import start_agent
from sova.dashboard.services.queue_service import get_priority_queue

router = APIRouter(tags=["queue"])


class StartFromQueueRequest(BaseModel):
    role: str | None = None
    force: bool = False

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip().lower()
        if not value:
            raise ValueError("role cannot be empty")
        return value


class BatchRequest(BaseModel):
    issues: list[str] = Field(..., min_length=1)
    action: Literal["run", "triage", "harden"]
    options: dict = Field(default_factory=dict)

    @field_validator("issues")
    @classmethod
    def validate_issues(cls, v: list[str]) -> list[str]:
        cleaned = []
        for issue in v:
            value = issue.strip()
            if not value:
                raise ValueError("issue values cannot be empty")
            if not value.isdigit():
                raise ValueError(f"invalid issue {value!r}: must be numeric")
            cleaned.append(value)
        return cleaned


@router.get("/queue")
async def queue() -> dict:
    """Get the priority-sorted issue queue."""
    project_dir = get_project_dir()
    items = await get_priority_queue(project_dir)
    return {"queue": items}


@router.post("/queue/start/{issue}")
async def start_from_queue(req: StartFromQueueRequest, issue: str = Path(..., pattern=r"^\d+$")) -> dict:
    """Start an agent run for an issue from the queue."""
    result = await start_agent(issue=issue, role=req.role, force=req.force)
    if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
        raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Agent start failed")
    return result


@router.post("/queue/batch")
async def start_batch(req: BatchRequest) -> dict:
    """Start a batch action on selected issues."""
    project_dir = get_project_dir()

    if req.action == "run":
        return await batch_service.start_batch_run(req.issues, project_dir)

    batch_id = batch_service.start_batch(
        action=req.action,
        issue_ids=req.issues,
        project_dir=project_dir,
        options=req.options,
    )
    return {"batch_id": batch_id, "status": "started", "count": len(req.issues)}


@router.get("/queue/batch/active")
async def active_batch() -> dict:
    """Return the currently running batch for the current project, if any."""
    project_dir = get_project_dir()
    active = batch_service.get_active_batch(project_dir)
    if active is None:
        return {"active": False}
    return {"active": True, "batch": active}


@router.get("/queue/batch/{batch_id}/status", responses={404: {"description": "Batch not found"}})
async def batch_status(batch_id: str) -> dict:
    """Get progress of a batch operation."""
    status = batch_service.get_batch_status(batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return status


@router.post("/queue/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str) -> dict:
    """Cancel a running batch operation."""
    cancelled = batch_service.cancel_batch(batch_id)
    return {"cancelled": cancelled}

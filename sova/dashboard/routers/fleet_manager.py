"""Fleet Manager API router: live fleet status and slot adjustment."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sova.dashboard.services.fleet_manager_service import FleetManagerService
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.fleet_manager")

router = APIRouter(prefix="/fleet-manager", tags=["fleet-manager"])

_service = FleetManagerService()


class SlotUpdateRequest(BaseModel):
    value: int = Field(ge=1, le=20)


@router.get("/status")
async def get_fleet_status() -> dict:
    """Return aggregated live fleet status across all projects."""
    status = await _service.get_fleet_status()
    return asdict(status)


@router.patch("/projects/{slug}/slots", responses={404: {"description": "Project not found"}})
async def update_project_slots(slug: str, req: SlotUpdateRequest) -> dict:
    """Update max_concurrent slots for a project."""
    ok = _service.set_max_concurrent(slug, req.value)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")
    return {"slug": slug, "max_concurrent": req.value}

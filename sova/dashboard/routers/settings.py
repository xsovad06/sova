"""Settings router -- config, invariants, personas."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import settings_service

router = APIRouter(tags=["settings"])


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


@router.get("/settings/config")
async def get_config():
    """Get the current project configuration."""
    project_dir = get_project_dir()
    return {"config": settings_service.get_config(project_dir)}


@router.post("/settings/config")
async def update_config(req: ConfigUpdateRequest):
    """Update a single configuration key."""
    project_dir = get_project_dir()
    return settings_service.update_config(project_dir, key=req.key, value=req.value)


@router.get("/settings/invariants")
async def list_invariants():
    """List invariant scripts."""
    project_dir = get_project_dir()
    return {"invariants": settings_service.list_invariants(project_dir)}


@router.get("/settings/personas")
async def list_personas():
    """List available personas and detected persona."""
    project_dir = get_project_dir()
    return {
        "personas": settings_service.list_personas(project_dir),
        "detected": settings_service.get_detected_persona(project_dir),
    }

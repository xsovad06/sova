"""Settings router -- config, invariants, personas."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import settings_service
from sova.dashboard.settings_meta import get_grouped_config
from sova.utils.logging import get_logger

router = APIRouter(tags=["settings"])
log = get_logger(component="dashboard.settings.router")


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


@router.get("/settings/config", responses={500: {"description": "Failed to fetch configuration"}})
async def get_config():
    """Get the current project configuration (flat, for backward compat)."""
    try:
        project_dir = get_project_dir()
        return {"config": settings_service.get_config(project_dir)}
    except Exception:
        log.warning("settings.config.get.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch configuration")


@router.get(
    "/settings/config/grouped",
    responses={500: {"description": "Failed to fetch configuration"}},
)
async def get_config_grouped():
    """Get configuration organized into labeled groups with descriptions."""
    try:
        project_dir = get_project_dir()
        flat = settings_service.get_config(project_dir)
        return {"groups": get_grouped_config(flat)}
    except Exception:
        log.warning("settings.config.grouped.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch configuration")


@router.post("/settings/config", responses={500: {"description": "Failed to update configuration"}})
async def update_config(req: ConfigUpdateRequest):
    """Update a single configuration key."""
    try:
        project_dir = get_project_dir()
        return settings_service.update_config(project_dir, key=req.key, value=req.value)
    except Exception:
        log.warning("settings.config.update.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update configuration")


@router.get("/settings/invariants", responses={500: {"description": "Failed to fetch invariants"}})
async def list_invariants():
    """List invariant scripts."""
    try:
        project_dir = get_project_dir()
        return {"invariants": settings_service.list_invariants(project_dir)}
    except Exception:
        log.warning("settings.invariants.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch invariants")


@router.get("/settings/personas", responses={500: {"description": "Failed to fetch personas"}})
async def list_personas():
    """List available personas and detected persona."""
    try:
        project_dir = get_project_dir()
        return {
            "personas": settings_service.list_personas(project_dir),
            "detected": settings_service.get_detected_persona(project_dir),
        }
    except Exception:
        log.warning("settings.personas.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch personas")

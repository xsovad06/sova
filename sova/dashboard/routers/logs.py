"""Logs router -- filterable log viewer."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.log_service import get_components, get_logs
from sova.utils.logging import get_logger

router = APIRouter(tags=["logs"])
log = get_logger(component="dashboard.logs")


@router.get("/logs", responses={500: {"description": "Failed to fetch logs"}})
async def logs(
    level: str = "",
    component: str = "",
    search: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Get filtered log entries."""
    try:
        project_dir = get_project_dir()
        return await get_logs(
            project_dir,
            level=level,
            component=component,
            search=search,
            limit=limit,
            offset=offset,
        )
    except Exception:
        log.warning("logs.query.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch logs")


@router.get("/logs/components", responses={500: {"description": "Failed to fetch log components"}})
async def log_components():
    """Get distinct component names for the filter dropdown."""
    try:
        project_dir = get_project_dir()
        return {"components": await get_components(project_dir)}
    except Exception:
        log.warning("logs.components.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch log components")

"""Logs router -- filterable log viewer."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.log_service import get_components, get_logs

router = APIRouter(tags=["logs"])


@router.get("/logs")
async def logs(
    level: str = "",
    component: str = "",
    search: str = "",
    limit: int = 200,
    offset: int = 0,
):
    """Get filtered log entries."""
    project_dir = get_project_dir()
    return get_logs(
        project_dir,
        level=level,
        component=component,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/logs/components")
async def log_components():
    """Get distinct component names for the filter dropdown."""
    project_dir = get_project_dir()
    return {"components": get_components(project_dir)}

"""Health API: read-only diagnostic checks for the settings page panel."""

from __future__ import annotations

from fastapi import APIRouter

from sova.config.context import resolve_project_dir
from sova.dashboard.services import health_service
from sova.dashboard.services.agent_pool import get_default_project_dir

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/checks")
async def checks() -> dict:
    """Run every diagnostic check (sova.utils.diagnostics) and return the results.

    Never 500s: health_service.get_health_checks() converts any internal
    failure into a single failed check row rather than raising.
    """
    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())
    return await health_service.get_health_checks(project_dir)

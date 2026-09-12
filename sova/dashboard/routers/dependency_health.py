"""Dependency Health API: outdated, deprecated, and vulnerable dependency tracking."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.dashboard.services import dependency_health_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.logging import get_logger

router = APIRouter(prefix="/dependency-health", tags=["dependency-health"])
log = get_logger(component="dashboard.api.dependency_health")

_ERROR_RESPONSES = {400: {"description": "No project selected"}, 500: {"description": "Internal error"}}


async def _get_snapshot(*, force_refresh: bool = False) -> dependency_health_service.DependencyHealthSnapshot:
    """Load config and return the project's snapshot, translating any failure into a 500.

    `get_project_dir()` only returns a value when the multi-project middleware set it
    from a `/p/{slug}/` prefix; in single-project mode it is always None, so the
    single-project default (set at startup) is the fallback before treating this as
    "no project selected".
    """
    project_dir = get_project_dir() or get_default_project_dir()
    if project_dir is None:
        raise HTTPException(status_code=400, detail="No project selected")
    try:
        dh_cfg = load_config(project_dir).dependency_health
        return await dependency_health_service.get_snapshot(
            project_dir,
            enabled=dh_cfg.enabled,
            cache_ttl_minutes=dh_cfg.cache_ttl_minutes,
            registry_timeout_seconds=dh_cfg.registry_timeout_seconds,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        log.warning("dependency_health.snapshot_failed", force_refresh=force_refresh, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch dependency health data") from exc


@router.get("/snapshot", responses=_ERROR_RESPONSES)
async def snapshot() -> dict:
    return dependency_health_service.snapshot_to_dict(await _get_snapshot())


@router.get("/packages", responses=_ERROR_RESPONSES)
async def packages() -> dict:
    result = await _get_snapshot()
    return {"packages": [dependency_health_service.package_to_dict(p) for p in result.packages]}


@router.get(
    "/packages/{name:path}",
    responses={404: {"description": "Package not found"}, **_ERROR_RESPONSES},
)
async def package_detail(name: str) -> dict:
    result = await _get_snapshot()
    for pkg in result.packages:
        if pkg.name == name:
            return dependency_health_service.package_to_dict(pkg)
    raise HTTPException(status_code=404, detail="Package not found")


@router.post("/refresh", responses=_ERROR_RESPONSES)
async def refresh() -> dict:
    return dependency_health_service.snapshot_to_dict(await _get_snapshot(force_refresh=True))

"""Dependency Health API: outdated, deprecated, and vulnerable dependency tracking."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import dependency_health_service

router = APIRouter(prefix="/dependency-health", tags=["dependency-health"])

_ERROR_RESPONSES = {
    400: {"description": "No project selected"},
    422: {"description": "A package manifest is not valid UTF-8"},
    500: {"description": "Internal error"},
}


async def _load_snapshot(*, force_refresh: bool = False) -> dependency_health_service.DependencyHealthSnapshot:
    """The project's snapshot, translating the service's two expected failures into their HTTP statuses."""
    try:
        return await dependency_health_service.get_project_snapshot(force_refresh=force_refresh)
    except dependency_health_service.NoProjectSelectedError as exc:
        raise HTTPException(status_code=400, detail="No project selected") from exc
    except dependency_health_service.ManifestUnreadableError as exc:
        # Not a server fault and not retryable: name the file so the user knows what to re-save as UTF-8.
        raise HTTPException(status_code=422, detail=f"Cannot scan dependencies: {exc.path} is not valid UTF-8") from exc


@router.get("/snapshot", responses=_ERROR_RESPONSES)
async def snapshot() -> dict:
    return dependency_health_service.snapshot_to_dict(await _load_snapshot())


@router.get("/packages", responses=_ERROR_RESPONSES)
async def packages() -> dict:
    result = await _load_snapshot()
    return {"packages": [dependency_health_service.package_to_dict(p) for p in result.packages]}


@router.get(
    "/packages/{name:path}",
    responses={404: {"description": "Package not found"}, **_ERROR_RESPONSES},
)
async def package_detail(name: str) -> dict:
    result = await _load_snapshot()
    for pkg in result.packages:
        if pkg.name == name:
            return dependency_health_service.package_to_dict(pkg)
    raise HTTPException(status_code=404, detail="Package not found")


@router.post("/refresh", responses=_ERROR_RESPONSES)
async def refresh() -> dict:
    return dependency_health_service.snapshot_to_dict(await _load_snapshot(force_refresh=True))

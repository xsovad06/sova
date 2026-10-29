"""Dependency Health API: outdated, deprecated, and vulnerable dependency tracking."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from sova.config.context import resolve_project_dir_or_none
from sova.config.loader import load_config
from sova.dashboard.services import dependency_health_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.dedup_log import FileVersion, forget_under, warn_once_per_version
from sova.utils.logging import get_logger

router = APIRouter(prefix="/dependency-health", tags=["dependency-health"])
log = get_logger(component="dashboard.api.dependency_health")

# resolved path -> (mtime, size) of the unreadable manifest version already warned about
_warned_manifests: dict[str, FileVersion] = {}

_ERROR_RESPONSES = {
    400: {"description": "No project selected"},
    422: {"description": "A package manifest is not valid UTF-8"},
    500: {"description": "Internal error"},
}


def _note_unreadable_manifest(path: Path) -> None:
    """Warn once per version of an undecodable manifest, then log at debug on repeats.

    The failure is not cached, so every page load rescans and reaches this again for the same file.
    A new save is a new version (mtime and size), so a still-broken re-save warns again. No traceback: the
    path and the exception's message already say everything, and the frames hold the manifest text.
    """
    warn_once_per_version(log, "dependency_health.manifest_unreadable", path, _warned_manifests)


async def _get_snapshot(*, force_refresh: bool = False) -> dependency_health_service.DependencyHealthSnapshot:
    """Load config and return the project's snapshot, translating any failure into a 500.

    The per-request project is only set by the multi-project middleware from a `/p/{slug}/`
    prefix; in single-project mode the single-project default (set at startup) is the fallback
    before treating this as "no project selected".
    """
    project_dir = resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    if project_dir is None:
        raise HTTPException(status_code=400, detail="No project selected")
    try:
        dh_cfg = load_config(project_dir).dependency_health
        snapshot = await dependency_health_service.get_snapshot(
            project_dir,
            enabled=dh_cfg.enabled,
            cache_ttl_minutes=dh_cfg.cache_ttl_minutes,
            registry_timeout_seconds=dh_cfg.registry_timeout_seconds,
            force_refresh=force_refresh,
        )
        # Every manifest scanned cleanly, so none of this project's warned versions is current any more.
        forget_under(project_dir, _warned_manifests)
        return snapshot
    except dependency_health_service.ManifestUnreadableError as exc:
        # Not a server fault and not retryable: name the file so the user knows what to re-save as UTF-8.
        _note_unreadable_manifest(exc.path)
        raise HTTPException(status_code=422, detail=f"Cannot scan dependencies: {exc.path} is not valid UTF-8") from exc
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

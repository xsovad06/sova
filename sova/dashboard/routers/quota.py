"""CodeRabbit quota API -- rate-limit status and sync."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.db.session import get_session
from sova.supervisor.coderabbit_quota import get_quota_status, sync_from_github
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.quota")


@router.get("/quota/coderabbit", responses={500: {"description": "Failed to fetch quota status"}})
async def coderabbit_quota():
    cfg = load_config(get_project_dir())
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False}

    try:
        async with await get_session() as session:
            status = await get_quota_status(
                session,
                cfg.coderabbit_quota,
                project_slug=cfg.github_repo,
            )
        return {
            "enabled": status.enabled,
            "reviews_in_window": status.reviews_in_window,
            "reviews_per_hour": status.reviews_per_hour,
            "can_create_pr": status.can_create_pr,
            "next_available_minutes": status.next_available_minutes,
            "window_minutes": status.window_minutes,
        }
    except Exception:
        log.warning("quota.coderabbit.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch CodeRabbit quota status")


@router.post("/quota/coderabbit/sync", responses={500: {"description": "Failed to sync quota data"}})
async def sync_coderabbit_quota():
    cfg = load_config(get_project_dir())
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False, "synced": 0}

    if not cfg.github_repo:
        return {"enabled": True, "synced": 0, "error": "No github_repo configured"}

    try:
        async with await get_session() as session:
            new_count = await sync_from_github(
                session,
                cfg.github_repo,
                cfg.coderabbit_quota,
                project_slug=cfg.github_repo,
                github_user=cfg.github_user,
            )
        return {"enabled": True, "synced": new_count}
    except Exception:
        log.warning("quota.coderabbit.sync_error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to sync CodeRabbit quota data")

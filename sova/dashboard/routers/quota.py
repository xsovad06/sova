"""CodeRabbit quota API: rate-limit status, sync, and PR queue."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.dashboard.config_errors import load_config_or_http
from sova.db.session import get_session
from sova.supervisor.coderabbit_quota import (
    get_quota_status,
    is_sync_stale,
    start_background_sync,
    sync_from_github,
)

router = APIRouter()


@router.get("/quota/coderabbit", responses={500: {"description": "Internal error"}})
async def coderabbit_quota() -> dict:
    """Answer from the stored count at once; a stale one starts a background sync instead of waiting for it.

    The sync starts only when CodeRabbit is a configured review tool (otherwise the tile is hidden and nobody
    reads the figures), and ``start_background_sync()`` holds it back after a failed attempt.
    """
    project_dir = get_project_dir()
    cfg = load_config_or_http(lambda: load_config(project_dir), event="quota.coderabbit.config_load_failed")
    configured = "coderabbit" in cfg.external_reviews.tools
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False, "configured": configured}

    refreshing = bool(configured and cfg.github_repo and is_sync_stale(cfg.github_repo))
    if refreshing:
        start_background_sync(
            cfg.github_repo,
            cfg.coderabbit_quota,
            project_dir=project_dir,
            project_slug=cfg.github_repo,
            github_user=cfg.github_user,
        )

    async with await get_session() as session:
        status = await get_quota_status(
            session,
            cfg.coderabbit_quota,
            project_slug=cfg.github_repo,
            repo=cfg.github_repo,
            fetch_stars=False,
        )
    return {
        "enabled": status.enabled,
        "configured": configured,
        "refreshing": refreshing,
        "reviews_in_window": status.reviews_in_window,
        "reviews_per_hour": status.reviews_per_hour,
        "can_create_pr": status.can_create_pr,
        "next_available_minutes": status.next_available_minutes,
        "window_minutes": status.window_minutes,
        "synced_at": status.synced_at,
        "auto_review_eligible": status.auto_review_eligible,
        "stars": status.stars,
        "limit_from_10_stars": status.limit_from_10_stars,
        "limit_scales_with_stars": status.limit_scales_with_stars,
        "limit_is_explicit": status.limit_is_explicit,
    }


@router.post("/quota/coderabbit/sync", responses={500: {"description": "Internal error"}})
async def sync_coderabbit_quota() -> dict:
    project_dir = get_project_dir()
    cfg = load_config_or_http(lambda: load_config(project_dir), event="quota.coderabbit_sync.config_load_failed")
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False, "synced": 0}

    if not cfg.github_repo:
        raise HTTPException(status_code=503, detail="No github_repo configured")

    async with await get_session() as session:
        new_count = await sync_from_github(
            session,
            cfg.github_repo,
            cfg.coderabbit_quota,
            project_slug=cfg.github_repo,
            github_user=cfg.github_user,
            force=True,
        )
    return {"enabled": True, "synced": new_count}


@router.get("/quota/pr-queue", responses={500: {"description": "Internal error"}})
async def pr_queue_status() -> dict:
    """Get the current PR creation queue status."""
    from sqlalchemy import func, select

    from sova.db.models import PRCreationQueue, PRQueueStatus

    cfg = load_config(get_project_dir())
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False, "pending": 0, "entries": []}

    async with await get_session() as session:
        # Count pending
        count_stmt = select(func.count()).where(
            PRCreationQueue.status == PRQueueStatus.PENDING,
            PRCreationQueue.project_slug == cfg.github_repo,
        )
        count_result = await session.execute(count_stmt)
        pending_count = count_result.scalar() or 0

        # Get recent entries (all non-cancelled)
        entries_stmt = (
            select(PRCreationQueue)
            .where(
                PRCreationQueue.project_slug == cfg.github_repo,
                PRCreationQueue.status != PRQueueStatus.CANCELLED,
            )
            .order_by(PRCreationQueue.enqueued_at.desc())
            .limit(20)
        )
        entries_result = await session.execute(entries_stmt)
        entries = [
            {
                "id": e.id,
                "issue_number": e.issue_number,
                "title": e.title,
                "status": e.status,
                "pr_number": e.pr_number,
                "enqueued_at": e.enqueued_at.isoformat() if e.enqueued_at else None,
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
            }
            for e in entries_result.scalars().all()
        ]

    return {"enabled": True, "pending": pending_count, "entries": entries}

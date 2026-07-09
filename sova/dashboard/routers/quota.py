"""CodeRabbit quota API -- rate-limit status, sync, and PR queue."""

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


@router.get("/quota/pr-queue", responses={500: {"description": "Failed to fetch PR queue status"}})
async def pr_queue_status():
    """Get the current PR creation queue status."""
    from sqlalchemy import func, select

    from sova.db.models import PRCreationQueue, PRQueueStatus

    cfg = load_config(get_project_dir())
    if not cfg.coderabbit_quota.enabled:
        return {"enabled": False, "pending": 0, "entries": []}

    try:
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
    except Exception as exc:
        log.warning("quota.pr_queue.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch PR queue status") from exc

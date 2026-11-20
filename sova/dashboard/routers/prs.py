"""PR tracker API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from sova.config.context import resolve_project_dir
from sova.config.loader import load_config
from sova.dashboard.config_errors import load_config_or_http
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.dashboard.services.pr_service import list_open_prs_with_state
from sova.git.pr import PRLookupError

router = APIRouter(prefix="/prs", tags=["prs"])


@router.get("/open", responses={502: {"description": "The GitHub open-PR listing failed"}})
async def get_open_prs() -> dict:
    """List all open PRs with computed lifecycle state.

    A failed GitHub listing is a 502, not ``{"prs": []}``: an empty list would tell the PR tracker
    there are no open PRs and replace its last good list.
    """
    try:
        prs = await list_open_prs_with_state(raise_on_error=True)
    except PRLookupError:
        raise HTTPException(status_code=502, detail="Failed to load open PRs") from None
    return {"prs": prs}


# PR Metrics endpoints


@router.get("/metrics/summary")
async def get_pr_metrics_summary(days: int = Query(90, ge=7, le=365)) -> dict:
    """Aggregate PR lifecycle metrics over the given time window."""
    from sova.dashboard.services import pr_metrics_service

    return await pr_metrics_service.get_summary(days=days)


@router.get("/metrics/trends")
async def get_pr_metrics_trends(days: int = Query(90, ge=7, le=365)) -> dict:
    """Monthly trend data for PR throughput and cycle time."""
    from sova.dashboard.services import pr_metrics_service

    trends = await pr_metrics_service.get_trends(days=days)
    return {"monthly": trends}


@router.get("/metrics/by-author")
async def get_pr_metrics_by_author(days: int = Query(90, ge=7, le=365)) -> dict:
    """Per-contributor PR metrics breakdown."""
    from sova.dashboard.services import pr_metrics_service

    authors = await pr_metrics_service.get_author_stats(days=days)
    return {"authors": authors}


@router.post("/metrics/backfill")
async def trigger_pr_backfill(days: int = Query(90, ge=7, le=365)) -> dict:
    """Backfill PR lifecycle events from GitHub history."""
    from sova.dashboard.services import pr_metrics_service

    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())
    cfg = load_config_or_http(lambda: load_config(project_dir), event="prs.backfill.config_error")

    if not cfg.github_repo:
        raise HTTPException(status_code=400, detail="No github_repo configured")

    count = await pr_metrics_service.backfill_pr_events(
        repo=cfg.github_repo,
        days=days,
        github_user=cfg.github_user,
    )
    return {"events_created": count, "days": days}

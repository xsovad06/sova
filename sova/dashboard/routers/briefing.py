"""Briefing API: dashboard page backing the awareness briefing."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.dashboard.services import awareness_service
from sova.utils.logging import get_logger

router = APIRouter(prefix="/briefing", tags=["briefing"])
log = get_logger(component="dashboard.briefing")


@router.get("")
async def get_briefing() -> dict:
    # Same window as /feed/briefing (start of day), so the page and the feed
    # card agree on what "the briefing" covers: both go through
    # awareness_service.get_briefing() so the payload shape never drifts.
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return await awareness_service.get_briefing(get_project_dir(), since=start_of_day)
    except Exception:  # noqa: BLE001 (briefing aggregates many providers; an empty briefing beats a 500)
        # Mirror /feed/briefing: never 500 the page for a degraded/misconfigured
        # awareness setup, render an empty-but-valid briefing instead.
        log.warning("briefing.get.error", exc_info=True)
        return awareness_service.empty_briefing()


@router.get("/providers", responses={500: {"description": "Failed to fetch provider status"}})
async def get_providers() -> dict:
    try:
        statuses = await awareness_service.get_provider_statuses(get_project_dir())
        return {"providers": statuses}
    except Exception:  # noqa: BLE001 (health-check aggregation over arbitrary provider plugins)
        log.warning("briefing.providers.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch provider status")


@router.post("/{item_id}/dismiss")
async def dismiss_item(item_id: str) -> dict:
    awareness_service.dismiss_item(get_project_dir(), item_id)
    return {"dismissed": True}

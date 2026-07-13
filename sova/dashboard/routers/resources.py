"""Resources API -- resource monitoring data for agent runs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.services import resource_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.resources")


@router.get(
    "/resources/{run_id}/summary",
    responses={404: {"description": "Run not found"}, 500: {"description": "Internal error"}},
)
async def resource_summary(run_id: int):
    try:
        async with await get_session() as session:
            result = await resource_service.get_resource_summary(session, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result
    except HTTPException:
        raise
    except Exception:
        log.warning("resources.summary.error", run_id=run_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch resource summary") from None


@router.get(
    "/resources/{run_id}/samples",
    responses={404: {"description": "Run not found"}, 500: {"description": "Internal error"}},
)
async def resource_samples(run_id: int, limit: int = Query(default=500, ge=1, le=2000)):
    try:
        async with await get_session() as session:
            result = await resource_service.get_resource_samples(session, run_id, limit=limit)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result
    except HTTPException:
        raise
    except Exception:
        log.warning("resources.samples.error", run_id=run_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch resource samples") from None


@router.get("/resources/live/{run_id}", responses={500: {"description": "Internal error"}})
async def live_metrics(run_id: int):
    try:
        result = resource_service.get_live_metrics(run_id)
        if result is None:
            return {"run_id": run_id, "cpu_percent": None, "memory_rss_bytes": None}
        return result
    except Exception:
        log.warning("resources.live.error", run_id=run_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch live metrics") from None


@router.get("/resources/system", responses={500: {"description": "Internal error"}})
async def system_info():
    try:
        return resource_service.get_system_info()
    except Exception:
        log.warning("resources.system.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch system info") from None


@router.get("/resources/system/metrics", responses={500: {"description": "Internal error"}})
async def system_metrics():
    try:
        return await asyncio.to_thread(resource_service.get_system_metrics)
    except Exception:
        log.warning("resources.system_metrics.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch system metrics") from None

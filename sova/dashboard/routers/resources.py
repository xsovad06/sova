"""Resources API -- resource monitoring data for agent runs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import resource_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.resources")


@router.get("/resources/{run_id}/summary", responses={404: {"description": "Run not found"}})
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
        raise HTTPException(status_code=500, detail="Failed to fetch resource summary")


@router.get("/resources/{run_id}/samples", responses={404: {"description": "Run not found"}})
async def resource_samples(run_id: int, limit: int = 500):
    try:
        async with await get_session() as session:
            result = await resource_service.get_resource_samples(session, run_id, limit=min(limit, 2000))
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result
    except HTTPException:
        raise
    except Exception:
        log.warning("resources.samples.error", run_id=run_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch resource samples")


@router.get("/resources/live/{run_id}")
async def live_metrics(run_id: int):
    result = resource_service.get_live_metrics(run_id)
    if result is None:
        return {"run_id": run_id, "cpu_percent": None, "memory_rss_bytes": None}
    return result


@router.get("/resources/system")
async def system_info():
    return resource_service.get_system_info()

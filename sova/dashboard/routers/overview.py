"""Overview API -- aggregated dashboard data."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import cost_service, memory_service, run_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.overview")


@router.get("/overview", responses={500: {"description": "Failed to fetch overview data"}})
async def overview():
    try:
        async with await get_session() as session:
            runs = await run_service.get_run_summary(session)
            costs = await cost_service.get_summary(session)
            mem_count = await memory_service.get_memory_count(session)

        return {
            "runs": runs,
            "costs": costs,
            "memory_count": mem_count,
        }
    except Exception:
        log.warning("overview.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch overview data")

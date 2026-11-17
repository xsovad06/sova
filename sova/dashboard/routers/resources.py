"""Resources API: resource monitoring data for agent runs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.services import resource_service
from sova.db.session import get_session

router = APIRouter()


@router.get(
    "/resources/{run_id}/summary",
    responses={404: {"description": "Run not found"}, 500: {"description": "Internal error"}},
)
async def resource_summary(run_id: int) -> dict:
    async with await get_session() as session:
        result = await resource_service.get_resource_summary(session, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get(
    "/resources/{run_id}/samples",
    responses={404: {"description": "Run not found"}, 500: {"description": "Internal error"}},
)
async def resource_samples(run_id: int, limit: int = Query(default=500, ge=1, le=2000)) -> dict:
    async with await get_session() as session:
        result = await resource_service.get_resource_samples(session, run_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/resources/live/{run_id}", responses={500: {"description": "Internal error"}})
async def live_metrics(run_id: int) -> dict:
    result = resource_service.get_live_metrics(run_id)
    if result is None:
        return {"run_id": run_id, "cpu_percent": None, "memory_rss_bytes": None}
    return result


@router.get("/resources/system", responses={500: {"description": "Internal error"}})
async def system_info() -> dict:
    return resource_service.get_system_info()


@router.get("/resources/system/metrics", responses={500: {"description": "Internal error"}})
async def system_metrics() -> dict:
    return await asyncio.to_thread(resource_service.get_system_metrics)


@router.get("/resources/system/history", response_model=None, responses={500: {"description": "Internal error"}})
async def system_metrics_history() -> list[dict]:
    return resource_service.get_system_metrics_history()


@router.get("/resources/cross-project", responses={500: {"description": "Internal error"}})
async def cross_project_metrics() -> dict:
    from sova.config.context import get_project_dir

    project_dir = get_project_dir()
    return await asyncio.to_thread(resource_service.get_cross_project_metrics, project_dir)


@router.get("/resources/capacity", response_model=None, responses={500: {"description": "Internal error"}})
async def capacity_recommendation() -> dict:
    from sova.config.context import get_project_dir

    project_dir = get_project_dir()
    async with await get_session(project_dir=project_dir) as session:
        return await resource_service.get_capacity_recommendation(session, project_dir)


@router.get("/resources/energy/total", response_model=None, responses={500: {"description": "Internal error"}})
async def total_energy() -> dict:
    from sova.config.context import get_project_dir

    async with await get_session(project_dir=get_project_dir()) as session:
        return await resource_service.get_total_energy(session)

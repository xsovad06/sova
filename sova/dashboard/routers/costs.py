"""Costs API -- cost tracking from the database."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import cost_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.costs")


@router.get("/costs/summary", responses={500: {"description": "Failed to fetch cost summary"}})
async def cost_summary():
    try:
        async with await get_session() as session:
            return await cost_service.get_summary(session)
    except Exception as exc:
        log.warning("costs.summary.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch cost summary") from exc


@router.get("/costs/daily", responses={500: {"description": "Failed to fetch daily costs"}})
async def daily_costs(days: int = 14):
    try:
        async with await get_session() as session:
            return await cost_service.get_daily(session, min(days, 90))
    except Exception as exc:
        log.warning("costs.daily.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch daily costs") from exc


@router.get("/costs/by-issue", responses={500: {"description": "Failed to fetch costs by issue"}})
async def costs_by_issue():
    try:
        async with await get_session() as session:
            return await cost_service.get_by_issue(session)
    except Exception as exc:
        log.warning("costs.by_issue.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch costs by issue") from exc


@router.get("/costs/by-phase", responses={500: {"description": "Failed to fetch costs by phase"}})
async def costs_by_phase():
    try:
        async with await get_session() as session:
            return await cost_service.get_by_phase(session)
    except Exception as exc:
        log.warning("costs.by_phase.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch costs by phase") from exc


@router.get("/costs/by-model", responses={500: {"description": "Failed to fetch costs by model"}})
async def costs_by_model():
    try:
        async with await get_session() as session:
            return await cost_service.get_by_model(session)
    except Exception as exc:
        log.warning("costs.by_model.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch costs by model") from exc


@router.get("/costs/by-routing", responses={500: {"description": "Failed to fetch costs by routing"}})
async def costs_by_routing():
    try:
        async with await get_session() as session:
            return await cost_service.get_by_routing(session)
    except Exception as exc:
        log.warning("costs.by_routing.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch costs by routing") from exc

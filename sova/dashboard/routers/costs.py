"""Costs API -- cost tracking from the database."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.services import cost_service
from sova.db.session import get_session

router = APIRouter()


@router.get("/costs/summary")
async def cost_summary():
    async with await get_session() as session:
        return await cost_service.get_summary(session)


@router.get("/costs/daily")
async def daily_costs(days: int = 14):
    async with await get_session() as session:
        return await cost_service.get_daily(session, min(days, 90))


@router.get("/costs/by-issue")
async def costs_by_issue():
    async with await get_session() as session:
        return await cost_service.get_by_issue(session)


@router.get("/costs/by-phase")
async def costs_by_phase():
    async with await get_session() as session:
        return await cost_service.get_by_phase(session)


@router.get("/costs/by-model")
async def costs_by_model():
    async with await get_session() as session:
        return await cost_service.get_by_model(session)

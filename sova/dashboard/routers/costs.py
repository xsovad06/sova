"""Costs API: cost tracking from the database."""

from __future__ import annotations

from fastapi import APIRouter, Query

from sova.dashboard.services import cost_service
from sova.db.session import get_session

router = APIRouter()


@router.get("/costs/summary", response_model=None, responses={500: {"description": "Internal error"}})
async def cost_summary() -> dict:
    async with await get_session() as session:
        return await cost_service.get_summary(session)


@router.get("/costs/daily", responses={500: {"description": "Internal error"}})
async def daily_costs(days: int = Query(default=14, ge=1, le=90)) -> list[dict]:
    async with await get_session() as session:
        return await cost_service.get_daily(session, days)


@router.get("/costs/by-issue", response_model=None, responses={500: {"description": "Internal error"}})
async def costs_by_issue() -> list[dict]:
    async with await get_session() as session:
        return await cost_service.get_by_issue(session)


@router.get("/costs/by-phase", response_model=None, responses={500: {"description": "Internal error"}})
async def costs_by_phase() -> list[dict]:
    async with await get_session() as session:
        return await cost_service.get_by_phase(session)


@router.get("/costs/by-model", response_model=None, responses={500: {"description": "Internal error"}})
async def costs_by_model() -> list[dict]:
    async with await get_session() as session:
        return await cost_service.get_by_model(session)


@router.get(
    "/costs/by-routing",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def costs_by_routing() -> list[dict]:
    async with await get_session() as session:
        return await cost_service.get_by_routing(session)

"""Costs API — cost tracking data."""

from fastapi import APIRouter
from app.services import costs_service

router = APIRouter()


@router.get("/costs")
async def all_costs():
    return costs_service.get_all()


@router.get("/costs/summary")
async def cost_summary():
    return costs_service.get_summary()


@router.get("/costs/daily")
async def daily_costs(days: int = 14):
    return costs_service.get_daily_totals(min(days, 90))


@router.get("/costs/by-ticket")
async def costs_by_ticket():
    return costs_service.get_by_ticket()


@router.get("/costs/by-phase")
async def costs_by_phase():
    return costs_service.get_by_phase()

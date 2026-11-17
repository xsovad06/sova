"""Overview API: aggregated dashboard data."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.services import cost_service, run_service
from sova.db.session import get_session

router = APIRouter()


@router.get("/overview", response_model=None, responses={500: {"description": "Internal error"}})
async def overview() -> dict:
    async with await get_session() as session:
        runs = await run_service.get_run_summary(session)
        costs = await cost_service.get_summary(session)

    return {
        "runs": runs,
        "costs": costs,
    }

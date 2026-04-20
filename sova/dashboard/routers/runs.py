"""Runs API -- task run history from the database."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import run_service
from sova.db.session import get_session

router = APIRouter()


@router.get("/runs")
async def list_runs(limit: int = 50, status: str | None = None):
    async with await get_session() as session:
        runs = await run_service.list_runs(session, limit=limit, status=status)
    return {"runs": runs}


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    async with await get_session() as session:
        run = await run_service.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        steps = await run_service.get_run_steps(session, run_id)
    return {"run": run, "steps": steps}

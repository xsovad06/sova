"""Work API -- unified active tasks and run history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import work_service
from sova.db.session import get_session

router = APIRouter(tags=["work"])


@router.get("/work/active")
async def get_active():
    """Get non-terminal task runs with step progress."""
    async with await get_session() as session:
        async with session.begin():
            items = await work_service.get_active_work(session)
        return {"tasks": items}


@router.get("/work/active-grouped")
async def get_active_grouped():
    """Get non-terminal runs grouped by issue (latest run per issue + previous)."""
    async with await get_session() as session:
        async with session.begin():
            groups = await work_service.get_active_work_grouped(session)
        return {"issues": groups}


@router.get("/work/history")
async def get_history(status: str | None = None, role: str | None = None, limit: int = 50):
    """Get completed/failed task run history."""
    async with await get_session() as session:
        async with session.begin():
            items = await work_service.get_work_history(session, status=status, role=role, limit=limit)
        return {"tasks": items}


@router.get("/work/summary")
async def get_summary():
    """Get aggregate counts for overview cards."""
    async with await get_session() as session:
        async with session.begin():
            summary = await work_service.get_work_summary(session)
        return summary


@router.get("/work/issue/{issue_number}")
async def get_issue_runs(issue_number: str):
    """Get all runs for a specific issue."""
    async with await get_session() as session:
        async with session.begin():
            runs = await work_service.get_runs_for_issue(session, issue_number)
        return {"runs": runs}


@router.get("/work/{run_id}", responses={404: {"description": "Run not found"}})
async def get_detail(run_id: int):
    """Get a single run with step details and pipeline progress."""
    async with await get_session() as session:
        async with session.begin():
            detail = await work_service.get_work_detail(session, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return detail


@router.post("/work/{run_id}/mark-failed", responses={404: {"description": "Run not found"}})
async def mark_failed(run_id: int):
    """Mark a non-terminal run as failed and kill the agent process."""
    from sova.dashboard.services import control_service, run_service

    await control_service.stop_agent(run_id=run_id)

    async with await get_session() as session:
        async with session.begin():
            result = await run_service.mark_run_failed(session, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result

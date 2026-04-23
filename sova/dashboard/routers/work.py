"""Work API -- unified active tasks and run history."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.services import work_service
from sova.db.session import get_session

router = APIRouter(tags=["work"])


@router.get("/work/active")
async def get_active():
    """Get non-terminal task runs with step progress."""
    session = await get_session()
    try:
        async with session.begin():
            items = await work_service.get_active_work(session)
        return {"tasks": items}
    finally:
        await session.close()


@router.get("/work/active-grouped")
async def get_active_grouped():
    """Get non-terminal runs grouped by issue (latest run per issue + previous)."""
    session = await get_session()
    try:
        async with session.begin():
            groups = await work_service.get_active_work_grouped(session)
        return {"issues": groups}
    finally:
        await session.close()


@router.get("/work/history")
async def get_history(status: str | None = None, role: str | None = None, limit: int = 50):
    """Get completed/failed task run history."""
    session = await get_session()
    try:
        async with session.begin():
            items = await work_service.get_work_history(session, status=status, role=role, limit=limit)
        return {"tasks": items}
    finally:
        await session.close()


@router.get("/work/summary")
async def get_summary():
    """Get aggregate counts for overview cards."""
    session = await get_session()
    try:
        async with session.begin():
            summary = await work_service.get_work_summary(session)
        return summary
    finally:
        await session.close()


@router.get("/work/issue/{issue_number}")
async def get_issue_runs(issue_number: str):
    """Get all runs for a specific issue."""
    session = await get_session()
    try:
        async with session.begin():
            runs = await work_service.get_runs_for_issue(session, issue_number)
        return {"runs": runs}
    finally:
        await session.close()


@router.get("/work/{run_id}")
async def get_detail(run_id: int):
    """Get a single run with step details and pipeline progress."""
    session = await get_session()
    try:
        async with session.begin():
            detail = await work_service.get_work_detail(session, run_id)
        if detail is None:
            return {"error": "Run not found"}
        return detail
    finally:
        await session.close()


@router.post("/work/{run_id}/mark-failed")
async def mark_failed(run_id: int):
    """Mark a non-terminal run as failed."""
    from sova.dashboard.services import run_service

    session = await get_session()
    try:
        async with session.begin():
            result = await run_service.mark_run_failed(session, run_id)
        if result is None:
            return {"error": "Run not found"}
        return result
    finally:
        await session.close()

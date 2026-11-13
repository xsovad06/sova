"""Work API: unified active tasks and run history."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from sova.config.context import get_project_dir
from sova.config.db_loader import set_issue_hidden
from sova.dashboard.services import work_board_service, work_service
from sova.dashboard.services.work_item_service import GraphConfigError
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.work")

router = APIRouter(tags=["work"])

_IssueNumber = Annotated[int, Path(gt=0)]


@router.get("/work/active")
async def get_active() -> dict:
    """Get non-terminal task runs with step progress."""
    project_dir = get_project_dir()
    async with await get_session() as session:
        async with session.begin():
            items = await work_service.get_active_work(session, project_dir)
        return {"tasks": items}


@router.get("/work/active-grouped")
async def get_active_grouped() -> dict:
    """Get non-terminal runs grouped by issue (latest run per issue + previous)."""
    project_dir = get_project_dir()
    async with await get_session() as session:
        async with session.begin():
            groups = await work_service.get_active_work_grouped(session, project_dir)
        return {"issues": groups}


@router.get("/work/history")
async def get_history(
    status: str | None = None,
    role: str | None = None,
    action: str | None = None,
    number: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Finished runs, newest first by start time, with the filtered count, cost and failed count.

    ``since`` and ``until`` are ISO 8601 timestamps the browser computes from the user's local dates (``since``
    inclusive, ``until`` exclusive), so the server needs no time zone.
    """
    async with await get_session() as session:
        async with session.begin():
            return await work_service.get_work_history(
                session,
                status=status,
                role=role,
                action=action,
                number=number,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )


@router.get("/work/summary")
async def get_summary() -> dict:
    """Get aggregate counts for overview cards."""
    project_dir = get_project_dir()
    async with await get_session() as session:
        async with session.begin():
            summary = await work_service.get_work_summary(session, project_dir)
        return summary


@router.get("/work/issue/{issue_number}")
async def get_issue_runs(issue_number: str) -> dict:
    """Get all runs for a specific issue."""
    async with await get_session() as session:
        async with session.begin():
            runs = await work_service.get_runs_for_issue(session, issue_number)
        return {"runs": runs}


@router.get(
    "/work/board",
    responses={
        500: {"description": "Failed to build the board"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_board(milestone: str = "") -> dict:
    """Every issue in exactly one column: up next, queued, running, review, needs, yours, blocked, done today.

    Declared before ``/work/{run_id}`` so ``board`` is not parsed as a run id. ``milestone`` limits the
    ranking behind ``up_next`` only. A failure is an error status, never an empty board.
    """
    try:
        return await work_board_service.build_board(get_project_dir(), milestone)
    except GraphConfigError:
        log.error("work.board_config_unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.error("work.board_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build the board")


@router.post("/work/hidden/{number}", responses={503: {"description": "Failed to save hidden issues"}})
async def hide_issue(number: _IssueNumber) -> dict:
    """Leave an issue out of Up next. Idempotent; changes no label, tracker state, queue entry or decision."""
    return await _set_hidden(number, hidden=True)


@router.delete("/work/hidden/{number}", responses={503: {"description": "Failed to save hidden issues"}})
async def unhide_issue(number: _IssueNumber) -> dict:
    """Let a hidden issue back into Up next. Idempotent: an issue that is not hidden is a success."""
    return await _set_hidden(number, hidden=False)


async def _set_hidden(number: int, *, hidden: bool) -> dict:
    try:
        return {"hidden": await set_issue_hidden(get_project_dir(), number, hidden=hidden)}
    except Exception:  # noqa: BLE001 (HTTP boundary: a settings write failure becomes a 503)
        log.error("work.hidden_issues_save_failed", issue=number, hidden=hidden, exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to save hidden issues")


@router.get("/work/{run_id}", responses={404: {"description": "Run not found"}})
async def get_detail(run_id: int) -> dict:
    """Get a single run with step details and pipeline progress."""
    project_dir = get_project_dir()
    async with await get_session() as session:
        async with session.begin():
            detail = await work_service.get_work_detail(session, run_id, project_dir)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return detail


@router.post("/work/issue/{issue_number}/rebase")
async def trigger_rebase(issue_number: int) -> dict:
    """Trigger an auto-rebase attempt for the PR linked to an issue."""
    from sova.db.session import get_session_factory
    from sova.supervisor.rebase import attempt_auto_rebase

    project_dir = get_project_dir()
    session_factory = await get_session_factory(project_dir)
    return await attempt_auto_rebase(issue_number, project_dir, session_factory)


@router.post("/work/{run_id}/mark-failed", responses={404: {"description": "Run not found"}})
async def mark_failed(run_id: int) -> dict:
    """Mark a non-terminal run as failed and kill the agent process."""
    from sova.dashboard.services import control_service, run_service

    await control_service.stop_agent(run_id=run_id)

    async with await get_session() as session:
        async with session.begin():
            result = await run_service.mark_run_failed(session, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result

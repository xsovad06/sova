"""Work API: unified active tasks and run history."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path as PathType
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy.exc import SQLAlchemyError

from sova.config.context import get_project_dir
from sova.config.db_loader import set_issue_hidden
from sova.dashboard.services import action_registry, work_board_service, work_service
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
        500: {"description": "Internal error"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_board(milestone: str = "", owner: Literal["all", "mine"] = "all") -> dict:
    """Every issue in exactly one column: up next, queued, running, review, needs, yours, blocked, done today.

    Declared before ``/work/{run_id}`` so ``board`` is not parsed as a run id. ``milestone`` limits the
    ranking behind ``up_next`` only. A failure is an error status, never an empty board.
    """
    try:
        return await work_board_service.build_board(get_project_dir(), milestone, owner=owner)
    except GraphConfigError:
        log.error("work.board_config_unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")


@router.get(
    "/work/search",
    responses={
        500: {"description": "Internal error"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def search_issues(q: Annotated[str, Query(max_length=200)] = "") -> dict:
    """Open issues matching ``q`` (a number or words of the title), on the board or not, newest first.

    Declared before ``/work/{run_id}`` so ``search`` is not parsed as a run id.
    """
    try:
        return await work_board_service.search_issues(get_project_dir(), q)
    except GraphConfigError:
        log.error("work.search_config_unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")


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
    except (OSError, SQLAlchemyError) as exc:
        log.error("work.hidden_issues_save_failed", issue=number, hidden=hidden, exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to save hidden issues") from exc


@router.get("/work/actions")
async def get_actions() -> dict:
    """The dashboard actions still running and the recent results, for each card's status line.

    Declared before ``/work/{run_id}`` so ``actions`` is not parsed as a run id.
    """
    return {"actions": action_registry.list_actions(get_project_dir())}


@router.delete("/work/actions/{action_id}", responses={404: {"description": "No finished action with this id"}})
async def dismiss_action(action_id: str) -> dict:
    """Dismiss a finished action's result (an error stays on its card until this)."""
    if not await action_registry.dismiss(get_project_dir(), action_id):
        raise HTTPException(status_code=404, detail="No finished action with this id")
    return {"dismissed": action_id}


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


# A background rebase must outlive the request that started it: the event loop keeps only weak references.
_rebase_tasks: set[asyncio.Task[None]] = set()


@router.post("/work/issue/{issue_number}/rebase", status_code=202)
async def trigger_rebase(issue_number: _IssueNumber) -> dict:
    """Start an auto-rebase of the PR linked to an issue, in the background, and answer at once.

    Conflict resolution can take minutes, so the request returns the action's id and the card follows the
    action through ``GET /work/actions``. A second click while one runs gets the running action back.
    """
    # Resolve now: the background task must not fall back to the global database or the working directory.
    project_dir = action_registry.resolve_project(get_project_dir())
    if project_dir is None:
        raise HTTPException(status_code=503, detail="No project is configured for this dashboard")
    running = action_registry.running_action(project_dir, "rebase", issue_number)
    if running is not None:
        return {"status": "running", "action": running}
    action_id = await action_registry.begin(project_dir, kind="rebase", label="Rebasing", issue=issue_number)
    task = asyncio.create_task(_run_rebase(issue_number, project_dir, action_id))
    _rebase_tasks.add(task)
    task.add_done_callback(_rebase_tasks.discard)
    return {"status": "started", "action_id": action_id}


async def _run_rebase(issue_number: int, project_dir: PathType, action_id: str) -> None:
    """Run the rebase, record its outcome, and drop the caches it made stale so the card shows the new state."""
    from sova.dashboard.services.agent_finalize import invalidate_work_item_caches
    from sova.db.session import get_session_factory
    from sova.supervisor.rebase import attempt_auto_rebase

    try:
        session_factory = await get_session_factory(project_dir)
        result = await attempt_auto_rebase(issue_number, project_dir, session_factory)
    except Exception as exc:  # noqa: BLE001 (background task: the failure is recorded on the card, never lost)
        log.error("work.rebase_failed", issue=issue_number, exc_info=True)
        await action_registry.finish(project_dir, action_id, ok=False, message=f"Rebase failed: {exc}")
        return
    ok, message = _rebase_outcome(result)
    invalidate_work_item_caches(project_dir, result.get("pr_number"))
    await action_registry.finish(project_dir, action_id, ok=ok, message=message)


def _rebase_outcome(result: dict) -> tuple[bool, str]:
    """(ok, message) for ``attempt_auto_rebase()``'s answer: a skip names its reason, a failure its error."""
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or result.get("error") or result.get("message") or "")
    if status == "success":
        return True, "Rebased"
    if status == "skipped":
        return True, f"Rebase skipped: {reason}" if reason else "Rebase skipped"
    return False, f"Rebase failed: {reason}" if reason else f"Rebase failed ({status or 'no status'})"


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

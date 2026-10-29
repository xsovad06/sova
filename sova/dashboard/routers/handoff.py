"""Handoff API: read, execute, and clear agent handoff actions."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from sova.dashboard.services import control_service, handoff_service
from sova.dashboard.services.agent_recovery import (
    get_synthesized_handoff,
    invalidate_synthesis_cache,
)

router = APIRouter()


async def _find_awaiting_approval_run(issue: str) -> int | None:
    """Find the most recent awaiting_approval TaskRun for an issue."""
    from sqlalchemy import select

    from sova.core.state import TaskStatus
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session, session.begin():
        stmt = (
            select(TaskRun.id)
            .where(
                TaskRun.issue_number == issue,
                TaskRun.status == TaskStatus.AWAITING_APPROVAL,
            )
            .order_by(TaskRun.started_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


class ExecuteActionRequest(BaseModel):
    action_id: str
    issue: str | None = None


class ClearHandoffRequest(BaseModel):
    issue: str | None = None


@router.get("/handoff")
async def get_handoff(issue: Annotated[str | None, Query()] = None) -> dict:
    """Get active handoff files, optionally filtered by issue.

    ``unreadable_handoffs`` lists the files that could not be parsed. ``schema_invalid_handoffs``
    lists the files that parse as JSON objects, and are still returned in ``handoffs`` because the
    dashboard renders legacy shapes, but fail ``DashboardHandoff`` validation: the supervisor and the
    auto-handoff skip them, so they must not look healthy. Without ``issue`` both cover the whole
    project; with ``issue`` they are narrowed to that issue's own file and the legacy shared
    ``handoff.json``, so another issue's corrupt file is not reported as this one's.
    """
    unreadable: list[str] = []
    schema_invalid: list[str] = []
    all_handoffs = handoff_service.get_all_handoffs(unreadable_out=unreadable, schema_invalid_out=schema_invalid)

    if issue:
        # Built inline rather than via handoff_filename(): this route is polled every few seconds
        # and that helper warns on an unsafe issue string, which here is client-controlled input.
        own_files = {f"handoff-{issue.lstrip('#').strip()}.json", "handoff.json"}
        unreadable = [path for path in unreadable if Path(path).name in own_files]
        schema_invalid = [path for path in schema_invalid if Path(path).name in own_files]
        filtered = [h for h in all_handoffs if str(h.get("issue", "")) == issue]
    else:
        filtered = all_handoffs

    problems = {"unreadable_handoffs": unreadable, "schema_invalid_handoffs": schema_invalid}

    if filtered:
        return {"has_handoff": True, "handoff": filtered[0], "handoffs": filtered, **problems}
    if issue:
        return {"has_handoff": False, "handoffs": [], **problems}

    synthesized = await get_synthesized_handoff()
    if synthesized is not None:
        return {"has_handoff": True, "handoff": synthesized, "handoffs": [synthesized], **problems}
    return {"has_handoff": False, "handoffs": [], **problems}


@router.post(
    "/handoff/execute",
    responses={
        404: {"description": "No active handoff or action not found"},
        400: {"description": "Unsupported execution type"},
    },
)
async def execute_handoff_action(req: ExecuteActionRequest) -> dict:
    """Execute a specific action from the current handoff.

    Finds the action by ID in next_actions, resolves execution params,
    archives the handoff, and starts the appropriate agent or command.
    Supports both file-backed and synthesized (PR state) handoffs.
    """
    # Search across all file-backed handoffs for the action
    all_handoffs = handoff_service.get_all_handoffs()
    handoff = None
    action = None
    norm_issue = str(req.issue).lstrip("#").strip() if req.issue else ""
    norm_pr = ""
    if norm_issue.startswith("pr:"):
        norm_pr = norm_issue.removeprefix("pr:")
        norm_issue = ""
    for h in all_handoffs:
        h_issue = str(h.get("issue") or "").lstrip("#").strip()
        h_pr = str(h.get("pr_number") or "")
        if norm_issue and h_issue != norm_issue:
            continue
        if norm_pr and h_pr != norm_pr:
            continue
        actions = h.get("next_actions", [])
        match = next(
            (a for a in actions if req.action_id in {a.get("id"), a.get("action"), a.get("command"), a.get("label")}),
            None,
        )
        if match:
            handoff = h
            action = match
            break

    if not handoff:
        synthesized = await get_synthesized_handoff()
        if synthesized:
            s_issue = str(synthesized.get("issue") or "").lstrip("#").strip()
            s_pr = str(synthesized.get("pr_number") or "")
            if (not norm_issue or s_issue == norm_issue) and (not norm_pr or s_pr == norm_pr):
                actions = synthesized.get("next_actions", [])
                match = next(
                    (
                        a
                        for a in actions
                        if req.action_id in {a.get("id"), a.get("action"), a.get("command"), a.get("label")}
                    ),
                    None,
                )
                if match:
                    handoff = synthesized
                    action = match

    # Third fallback: synthesize spec actions for awaiting_approval runs
    if not handoff and norm_issue and req.action_id in {"approve-spec", "reject-spec"}:
        run_id = await _find_awaiting_approval_run(norm_issue)
        if run_id is not None:
            if req.action_id == "approve-spec":
                result = await control_service.resume_from_approval(run_id)
            else:
                result = await control_service.reject_spec(run_id)
            if result.get("error") == "not_found":
                raise HTTPException(status_code=404, detail=result["detail"])
            if result.get("error") == "conflict":
                raise HTTPException(status_code=409, detail=result["detail"])
            if "error" in result:
                raise HTTPException(status_code=500, detail=result.get("detail", result["error"]))
            result["action"] = "Approve Spec" if req.action_id == "approve-spec" else "Reject"
            return result

    if not handoff or not action:
        raise HTTPException(status_code=404, detail=f"Action '{req.action_id}' not found in any handoff")

    exec_params = handoff_service.build_action_command(action)

    if handoff.get("source") != "pr-review-state":
        issue = str(handoff.get("issue") or "") or None
        handoff_service.clear_handoff(issue=issue)
    else:
        issue = str(handoff.get("issue") or "")
        pr = handoff.get("pr_number")
        if issue and pr is not None:
            invalidate_synthesis_cache(issue, pr)

    if exec_params["type"] == "agent":
        result = await control_service.start_agent(
            exec_params.get("issue") or "",
            role=exec_params.get("role"),
            pr_number=exec_params.get("pr_number"),
        )
        if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
            raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Action failed")
    elif exec_params["type"] == "claude-command":
        result = await control_service.start_command(
            exec_params["command"],
            exec_params.get("args", {}),
        )
        if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
            raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Action failed")
    elif exec_params["type"] == "shell":
        raise HTTPException(status_code=400, detail="Shell mode not yet supported in SOVA dashboard")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown execution type: {exec_params['type']}")

    result["action"] = action.get("label", req.action_id)
    return result


@router.post("/handoff/clear")
async def clear_handoff(req: ClearHandoffRequest | None = None) -> dict:
    """Archive and clear handoff file(s).

    With {"issue": "N"}: clears only that issue's handoff.
    Without body or null issue: clears all handoffs.
    """
    issue = req.issue if req else None
    cleared = handoff_service.clear_handoff(issue=issue)
    return {"cleared": cleared}

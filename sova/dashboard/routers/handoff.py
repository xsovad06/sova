"""Handoff API -- read, execute, and clear agent handoff actions."""

from __future__ import annotations

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
    """Get active handoff files, optionally filtered by issue."""
    all_handoffs = handoff_service.get_all_handoffs()

    if issue:
        filtered = [h for h in all_handoffs if str(h.get("issue", "")) == issue]
        if filtered:
            return {"has_handoff": True, "handoff": filtered[0], "handoffs": filtered}
        return {"has_handoff": False, "handoffs": []}

    if not all_handoffs:
        synthesized = await get_synthesized_handoff()
        if synthesized is not None:
            return {"has_handoff": True, "handoff": synthesized, "handoffs": [synthesized]}
        return {"has_handoff": False, "handoffs": []}

    return {
        "has_handoff": True,
        "handoff": all_handoffs[0],
        "handoffs": all_handoffs,
    }


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
    elif exec_params["type"] == "claude-command":
        result = await control_service.start_command(
            exec_params["command"],
            exec_params.get("args", {}),
        )
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

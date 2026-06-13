"""Handoff API -- read, execute, and clear agent handoff actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.services import control_service, handoff_service
from sova.dashboard.services.agent_recovery import (
    get_synthesized_handoff,
    invalidate_synthesis_cache,
)

router = APIRouter()


class ExecuteActionRequest(BaseModel):
    action_id: str
    issue: str | None = None


class ClearHandoffRequest(BaseModel):
    issue: str | None = None


@router.get("/handoff")
async def get_handoff() -> dict:
    """Get all active handoff files plus synthesized fallback."""
    all_handoffs = handoff_service.get_all_handoffs()

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
    norm_issue = req.issue.lstrip("#").strip() if req.issue else ""
    for h in all_handoffs:
        if norm_issue and h.get("issue", "").lstrip("#").strip() != norm_issue:
            continue
        actions = h.get("next_actions", [])
        match = next((a for a in actions if a.get("id") == req.action_id), None)
        if match:
            handoff = h
            action = match
            break

    if not handoff:
        synthesized = await get_synthesized_handoff()
        if synthesized:
            s_issue = synthesized.get("issue", "").lstrip("#").strip()
            if not norm_issue or s_issue == norm_issue:
                actions = synthesized.get("next_actions", [])
                match = next((a for a in actions if a.get("id") == req.action_id), None)
                if match:
                    handoff = synthesized
                    action = match

    if not handoff or not action:
        raise HTTPException(status_code=404, detail=f"Action '{req.action_id}' not found in any handoff")

    exec_params = handoff_service.build_action_command(action)

    if handoff.get("source") != "pr-review-state":
        issue = handoff.get("issue", "") or None
        handoff_service.clear_handoff(issue=issue)
    else:
        issue = handoff.get("issue", "")
        pr = handoff.get("pr_number")
        if issue and pr is not None:
            invalidate_synthesis_cache(issue, pr)

    if exec_params["type"] == "agent":
        result = await control_service.start_agent(
            exec_params.get("issue", ""),
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

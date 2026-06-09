"""Handoff API -- read, execute, and clear agent handoff actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.services import control_service, handoff_service

router = APIRouter()


class ExecuteActionRequest(BaseModel):
    action_id: str


@router.get("/handoff")
async def get_handoff():
    """Get the current handoff state."""
    handoff = handoff_service.get_handoff()
    if handoff is None:
        return {"has_handoff": False}
    return {"has_handoff": True, "handoff": handoff}


@router.post(
    "/handoff/execute",
    responses={
        404: {"description": "No active handoff or action not found"},
        400: {"description": "Unsupported execution type"},
    },
)
async def execute_handoff_action(req: ExecuteActionRequest):
    """Execute a specific action from the current handoff.

    Finds the action by ID in next_actions, resolves execution params,
    archives the handoff, and starts the appropriate agent or command.
    """
    handoff = handoff_service.get_handoff()
    if not handoff:
        raise HTTPException(status_code=404, detail="No active handoff")

    actions = handoff.get("next_actions", [])
    action = next((a for a in actions if a.get("id") == req.action_id), None)
    if not action:
        raise HTTPException(status_code=404, detail=f"Action '{req.action_id}' not found in handoff")

    exec_params = handoff_service.build_action_command(action)

    # Clear the current handoff before starting new work
    handoff_service.clear_handoff()

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
async def clear_handoff():
    """Archive and clear the current handoff."""
    cleared = handoff_service.clear_handoff()
    return {"cleared": cleared}

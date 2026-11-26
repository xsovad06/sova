"""Handoff API: read, execute, and clear agent handoff actions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from sova.dashboard.services import handoff_service

router = APIRouter()


class ExecuteActionRequest(BaseModel):
    action_id: str
    issue: str | None = None


class ClearHandoffRequest(BaseModel):
    issue: str | None = None


@router.get("/handoff")
async def get_handoff(issue: Annotated[str | None, Query()] = None) -> dict:
    """Get active handoff files, optionally filtered by issue.

    The project-wide read (no ``issue``) answers from the last build and rebuilds in the background when it is
    older than the snapshot TTL, because the synthesized handoff can wait on GitHub; a write here invalidates it.
    A read for one issue is always built on the spot.
    """
    if issue is not None:
        return await handoff_service.read_handoff(issue)
    return await handoff_service.get_handoff_snapshot()


_OUTCOME_STATUS = {
    handoff_service.ActionOutcome.NOT_FOUND: 404,
    handoff_service.ActionOutcome.REFUSED: 409,
    handoff_service.ActionOutcome.INVALID_INPUT: 400,
    handoff_service.ActionOutcome.UNSUPPORTED: 400,
    handoff_service.ActionOutcome.FAILED: 500,
}


@router.post(
    "/handoff/execute",
    responses={
        404: {"description": "No active handoff or action not found"},
        409: {"description": "The start was refused; the handoff stays pending"},
        400: {"description": "Unsupported execution type, or a command or argument the dashboard does not accept"},
        500: {"description": "The run record or spawn failed; the handoff stays pending"},
    },
)
async def execute_handoff_action(req: ExecuteActionRequest) -> dict:
    """Execute a specific action from a pending handoff (see ``handoff_service.execute_action()``)."""
    result = await handoff_service.execute_action(req.action_id, req.issue)
    if result.outcome is not handoff_service.ActionOutcome.STARTED:
        raise HTTPException(status_code=_OUTCOME_STATUS[result.outcome], detail=result.detail)
    return result.payload


@router.post("/handoff/clear")
async def clear_handoff(req: ClearHandoffRequest | None = None) -> dict:
    """Archive and clear handoff file(s).

    With {"issue": "N"}: clears only that issue's handoff.
    Without a body, or with a body that has no ``issue`` key: clears all handoffs. An ``issue`` that is present
    but empty (``null``, ``""``, ``"#"``) clears nothing, so a client that computed no issue cannot wipe them all.
    """
    if req is None or "issue" not in req.model_fields_set:
        cleared = handoff_service.clear_all_handoffs()
    else:
        cleared = handoff_service.clear_handoff(issue=req.issue)
    handoff_service.invalidate_handoff_snapshot()
    return {"cleared": cleared}

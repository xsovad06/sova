"""Spec API router -- read, approve, revise, skip, reject specs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.services import control_service, handoff_service, spec_service
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.spec")

router = APIRouter(prefix="/spec", tags=["spec"])


class ReviseRequest(BaseModel):
    feedback: str = ""


# -- Read endpoints -----------------------------------------------------------


@router.get("/pending")
async def list_pending() -> dict:
    """List all draft specs awaiting approval."""
    specs = spec_service.list_pending_specs()
    return {"specs": specs}


@router.get("/{issue_number}")
async def get_spec(issue_number: str) -> dict:
    """Get the spec for a specific issue."""
    spec = spec_service.read_spec(issue_number)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No spec found for issue #{issue_number}")
    return spec


# -- Action endpoints ---------------------------------------------------------


@router.post("/{issue_number}/approve")
async def approve_spec(issue_number: str) -> dict:
    """Approve a spec and resume pipeline (spawns developer agent)."""
    result = spec_service.approve_spec(issue_number)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Clear any spec-related handoff for this issue
    handoff_service.clear_handoff(issue=issue_number)

    # Spawn developer agent for the approved issue
    agent_result = await control_service.start_agent(issue_number, role="developer")
    result["agent"] = agent_result
    return result


@router.post("/{issue_number}/revise")
async def revise_spec(issue_number: str, req: ReviseRequest | None = None) -> dict:
    """Re-run spec generation with feedback.

    Note: feedback is not yet passed to the spawned agent (requires
    prompt-injection support in start_agent). The researcher re-runs
    /spec from scratch for now.
    """
    # Clear existing handoff
    handoff_service.clear_handoff(issue=issue_number)

    # Respawn researcher to re-run /spec
    agent_result = await control_service.start_agent(issue_number, role="researcher")
    return {"status": "revision_started", "agent": agent_result}


@router.post("/{issue_number}/skip")
async def skip_spec(issue_number: str) -> dict:
    """Skip spec review and proceed to development."""
    # Clear handoff
    handoff_service.clear_handoff(issue=issue_number)

    # Spawn developer without spec
    agent_result = await control_service.start_agent(issue_number, role="developer")
    return {"status": "skipped", "agent": agent_result}


@router.post("/{issue_number}/reject")
async def reject_spec(issue_number: str) -> dict:
    """Reject spec and mark issue as needs_spec."""
    result = spec_service.reject_spec(issue_number)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Clear handoff
    handoff_service.clear_handoff(issue=issue_number)
    return result

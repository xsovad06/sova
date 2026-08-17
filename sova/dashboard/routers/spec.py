"""Spec API router -- read, approve, revise, skip, reject specs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.config.context import get_project_dir
from sova.dashboard.services import control_service, handoff_service, spec_service
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.spec")

router = APIRouter(prefix="/spec", tags=["spec"])


async def _transition_to_researched(issue_number: str) -> None:
    """Transition issue to RESEARCHED so the developer Gate 3 check passes."""
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config

        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
        await adapter.transition_state(issue_number, TaskState.RESEARCHED)
        log.info("spec.transition_researched", issue=issue_number)
    except Exception:
        log.warning("spec.transition_researched_failed", issue=issue_number, exc_info=True)


class ApproveRequest(BaseModel):
    answers: dict[str, str] = {}


# -- Read endpoints -----------------------------------------------------------


@router.get("/pending")
async def list_pending() -> dict:
    """List all draft specs awaiting approval."""
    specs = spec_service.list_pending_specs()
    return {"specs": specs}


@router.get("/all")
async def list_all() -> dict:
    """List all specs (draft, approved, rejected)."""
    specs = spec_service.list_all_specs()
    return {"specs": specs}


@router.get("/{issue_number}", responses={404: {"description": "No spec found for this issue"}})
async def get_spec(issue_number: str) -> dict:
    """Get the spec for a specific issue."""
    spec = spec_service.read_spec(issue_number)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No spec found for issue #{issue_number}")
    return spec


# -- Action endpoints ---------------------------------------------------------


@router.post("/{issue_number}/approve", responses={404: {"description": "Spec not found or not in draft state"}})
async def approve_spec(issue_number: str, req: ApproveRequest | None = None) -> dict:
    """Approve a spec and resume pipeline (spawns developer agent)."""
    # Write answers into spec before approving
    answers = req.answers if req else {}
    if answers:
        spec_service.write_answers(issue_number, answers)

    result = spec_service.approve_spec(issue_number)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Transition the researcher's TaskRun from awaiting_approval to done
    updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "done")
    if updated_run_id:
        log.info("spec.approve.taskrun_completed", issue=issue_number, run_id=updated_run_id)

    await _transition_to_researched(issue_number)

    # Spawn developer agent, then clear handoff only on success
    try:
        agent_result = await control_service.start_agent(issue_number, role="developer")
    except Exception:
        log.warning("spec.approve.agent_spawn_failed", issue=issue_number, exc_info=True)
        raise
    if isinstance(agent_result, dict) and ("error" in agent_result or agent_result.get("status") == "error"):
        detail = agent_result.get("detail") or agent_result.get("error") or "Agent start failed"
        raise HTTPException(status_code=409, detail=detail)
    handoff_service.clear_handoff(issue=issue_number)
    result["agent"] = agent_result
    return result


@router.post("/{issue_number}/revise")
async def revise_spec(issue_number: str) -> dict:
    """Re-run spec generation. Respawns researcher to run /spec from scratch."""
    # Transition the researcher's TaskRun from awaiting_approval to rejected (spec sent back)
    updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "rejected")
    if updated_run_id:
        log.info("spec.revise.taskrun_rejected", issue=issue_number, run_id=updated_run_id)

    # Respawn researcher to re-run /spec, then clear handoff on success
    try:
        agent_result = await control_service.start_agent(issue_number, role="researcher")
    except Exception:
        log.warning("spec.revise.agent_spawn_failed", issue=issue_number, exc_info=True)
        raise
    if isinstance(agent_result, dict) and ("error" in agent_result or agent_result.get("status") == "error"):
        detail = agent_result.get("detail") or agent_result.get("error") or "Agent start failed"
        raise HTTPException(status_code=409, detail=detail)
    handoff_service.clear_handoff(issue=issue_number)
    return {"status": "revision_started", "agent": agent_result}


@router.post("/{issue_number}/skip")
async def skip_spec(issue_number: str) -> dict:
    """Skip spec review and proceed to development."""
    # Transition the researcher's TaskRun from awaiting_approval to done
    updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "done")
    if updated_run_id:
        log.info("spec.skip.taskrun_completed", issue=issue_number, run_id=updated_run_id)

    await _transition_to_researched(issue_number)
    # Spawn developer without spec, then clear handoff on success
    try:
        agent_result = await control_service.start_agent(issue_number, role="developer")
    except Exception:
        log.warning("spec.skip.agent_spawn_failed", issue=issue_number, exc_info=True)
        raise
    if isinstance(agent_result, dict) and ("error" in agent_result or agent_result.get("status") == "error"):
        detail = agent_result.get("detail") or agent_result.get("error") or "Agent start failed"
        raise HTTPException(status_code=409, detail=detail)
    handoff_service.clear_handoff(issue=issue_number)
    return {"status": "skipped", "agent": agent_result}


@router.post("/{issue_number}/reject", responses={404: {"description": "Spec not found"}})
async def reject_spec(issue_number: str) -> dict:
    """Reject spec."""
    result = spec_service.reject_spec(issue_number)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Transition the researcher's TaskRun from awaiting_approval to rejected
    updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "rejected")
    if updated_run_id:
        log.info("spec.reject.taskrun_rejected", issue=issue_number, run_id=updated_run_id)

    handoff_service.clear_handoff(issue=issue_number)
    return result

"""Spec API router: read, approve, revise, skip, reject specs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.adapters.base import AdapterError, TaskAdapter, TaskState
from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.dashboard.config_errors import load_config_or_http
from sova.dashboard.services import control_service, handoff_service, spec_service
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.spec")

router = APIRouter(prefix="/spec", tags=["spec"])

# One lock per issue keeps /revise's tracker move, researcher start and rollback ordered, so a second request that
# overlaps the first cannot restore a state the first one's researcher now owns. It is not ``pa._lock``: that guards
# the launch slots and is held only inside ``start_agent()``, never across the tracker writes around it.
_revise_locks: dict[str, asyncio.Lock] = {}


def _tracker(event: str) -> TaskAdapter:
    """The project's tracker adapter. A config that cannot be loaded answers by its one mapping."""
    from sova.adapters import create_adapter

    project_dir = get_project_dir()
    return create_adapter(load_config_or_http(lambda: load_config(project_dir), event=event))


async def _transition_issue(issue_number: str, state: TaskState) -> None:
    """Move the issue to *state* on the tracker before an agent starts on it.

    /approve and /skip move it to RESEARCHED so the developer Gate 3 check passes; /revise moves it back to
    TRIAGED, the only state the researcher accepts. A config that cannot be loaded answers by its one mapping
    (``load_config_or_http()``). A failed tracker write is a 502 with a fixed detail, so no agent is started on an
    issue the tracker shows in the wrong state.
    """
    try:
        await _tracker("spec.transition_config_error").transition_state(issue_number, state)
    except (AdapterError, RuntimeError, OSError) as exc:
        log.warning("spec.transition_failed", issue=issue_number, state=str(state), exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"Failed to transition issue #{issue_number} to {state.name}"
        ) from exc
    log.info("spec.transition", issue=issue_number, state=str(state))


async def _issue_state(issue_number: str) -> TaskState:
    """The issue's tracker state; a failed read is a 502 with a fixed detail, as a failed write is."""
    try:
        return await _tracker("spec.transition_config_error").get_state(issue_number)
    except (AdapterError, RuntimeError, OSError) as exc:
        log.warning("spec.state_read_failed", issue=issue_number, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to read the state of issue #{issue_number}") from exc


async def _restore_state(issue_number: str, state: TaskState) -> None:
    """Undo /revise's move to TRIAGED after a failed start. A failure is logged, never raised over the start's.

    Only a state still TRIAGED is put back: one that moved on meanwhile (a running agent advanced it) is that
    agent's and stays.
    """
    try:
        current = await _issue_state(issue_number)
        if current != TaskState.TRIAGED:
            log.warning("spec.revise.restore_skipped", issue=issue_number, state=str(current))
            return
        await _transition_issue(issue_number, state)
    except HTTPException:
        log.error("spec.revise.restore_failed", issue=issue_number, state=str(state), exc_info=True)


class ApproveRequest(BaseModel):
    answers: dict[str, str] = {}


# Read endpoints -----------------------------------------------------------


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
    spec["drift"] = await spec_service.spec_drift(
        spec.get("raw_content") or "", spec.get("status") or "draft", issue_number=issue_number
    )
    return spec


# Action endpoints ---------------------------------------------------------


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

    await _transition_issue(issue_number, TaskState.RESEARCHED)

    # Spawn developer agent, then clear handoff only on success
    agent_result = await control_service.start_agent(issue_number, role="developer")
    if isinstance(agent_result, dict) and ("error" in agent_result or agent_result.get("status") == "error"):
        detail = agent_result.get("detail") or agent_result.get("error") or "Agent start failed"
        raise HTTPException(status_code=409, detail=detail)
    handoff_service.clear_handoff(issue=issue_number)
    result["agent"] = agent_result
    return result


@router.post(
    "/{issue_number}/revise",
    responses={
        409: {"description": "The researcher could not be started"},
        502: {"description": "The tracker state could not be read or changed"},
    },
)
async def revise_spec(issue_number: str) -> dict:
    """Re-run spec generation. Respawns researcher to run /spec from scratch.

    A spec awaiting review belongs to a RESEARCHED issue (the researcher moves it there once the spec is drafted),
    and the researcher accepts only TRIAGED ones, so sending a spec back moves the issue to TRIAGED first. A start
    that fails puts back the state the issue had, which is not always RESEARCHED: an approved spec's issue can be
    in progress. No ``force``: it would also skip the memory, network and duplicate-run checks, so a double click
    could start two researchers on one issue.
    """
    async with _revise_locks.setdefault(issue_number, asyncio.Lock()):
        # Read the tracker first: a failed read is a 502 that leaves the awaiting-approval run untouched.
        prior = await _issue_state(issue_number)

        # An agent already running on the issue refuses the start, so refuse before any state is written.
        conflict = await control_service.issue_conflict(issue_number)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict.get("error") or "Agent start failed")

        moved = prior != TaskState.TRIAGED
        if moved:
            await _transition_issue(issue_number, TaskState.TRIAGED)

        # Respawn researcher to re-run /spec, then reject the awaiting-approval run and clear handoff on success
        try:
            agent_result = await control_service.start_agent(issue_number, role="researcher")
        except Exception:
            if moved:
                await _restore_state(issue_number, prior)
            raise
        if isinstance(agent_result, dict) and ("error" in agent_result or agent_result.get("status") == "error"):
            if moved:
                await _restore_state(issue_number, prior)
            detail = agent_result.get("detail") or agent_result.get("error") or "Agent start failed"
            raise HTTPException(status_code=409, detail=detail)

        # Transition the researcher's TaskRun from awaiting_approval to rejected (spec sent back), only once the
        # revision started: a refused start leaves the run awaiting approval, matching the issue's restored state.
        updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "rejected")
        if updated_run_id:
            log.info("spec.revise.taskrun_rejected", issue=issue_number, run_id=updated_run_id)
        handoff_service.clear_handoff(issue=issue_number)
        return {"status": "revision_started", "agent": agent_result}


@router.post("/{issue_number}/skip")
async def skip_spec(issue_number: str) -> dict:
    """Skip spec review and proceed to development."""
    # Transition the researcher's TaskRun from awaiting_approval to done
    updated_run_id = await control_service.complete_awaiting_approval_by_issue(issue_number, "done")
    if updated_run_id:
        log.info("spec.skip.taskrun_completed", issue=issue_number, run_id=updated_run_id)

    await _transition_issue(issue_number, TaskState.RESEARCHED)
    # Spawn developer without spec, then clear handoff on success
    agent_result = await control_service.start_agent(issue_number, role="developer")
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

"""Agents API -- multi-agent start, stop, status, and output streaming."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, field_validator

from sova.dashboard.services import control_service
from sova.utils.logging import get_logger

router = APIRouter(tags=["agents"])
log = get_logger(component="dashboard.agents")


class StartAgentRequest(BaseModel):
    issue: str = ""
    role: str | None = None
    force: bool = False
    resume_run_id: int | None = None
    pr_number: int | None = None

    @field_validator("issue")
    @classmethod
    def validate_issue(cls, v: str) -> str:
        value = v.strip()
        if value and not value.isdigit():
            raise ValueError("issue must be a numeric string or empty for issue-less runs")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip().lower()
        if not value:
            raise ValueError("role cannot be empty")
        return value


class RunCommandRequest(BaseModel):
    command: str
    args: dict | None = None


@router.get("/agents/active")
async def get_active_agents():
    """Get all running + recently completed agents (dashboard + external)."""
    return await control_service.get_unified_agents()


@router.get("/agents/interrupted")
async def interrupted_runs():
    """Get recently interrupted runs (from dashboard crash/restart)."""
    from sova.dashboard.services.agent_recovery import get_interrupted_runs

    runs = await get_interrupted_runs()
    return {"interrupted": runs}


@router.post("/agents/interrupted/dismiss")
async def dismiss_interrupted():
    """Mark all interrupted runs as failed so they no longer show in the banner."""
    from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

    count = await dismiss_interrupted_runs()
    return {"dismissed": count}


@router.get("/agents/pipeline")
async def get_pipeline():
    """Get the developer pipeline step names."""
    return {"steps": control_service.DEVELOPER_PIPELINE}


@router.get("/agents/{run_id}/output")
async def get_agent_output(run_id: int, since: int = 0):
    """Get output lines for a specific agent."""
    lines = control_service.get_output(since, run_id=run_id)
    return {"lines": lines, "total": since + len(lines)}


@router.post("/agents/start")
async def start_agent(req: StartAgentRequest):
    """Start a new agent process."""
    if not req.issue and not req.role:
        return {"error": "Either issue or role is required for starting an agent"}
    return await control_service.start_agent(
        req.issue,
        role=req.role,
        force=req.force,
        resume_run_id=req.resume_run_id,
        pr_number=req.pr_number,
    )


@router.post("/agents/{run_id}/stop")
async def stop_agent(run_id: int):
    """Stop a specific running agent."""
    return await control_service.stop_agent(run_id=run_id)


@router.get("/agents/issue/{issue_number}/pr-status")
async def get_issue_pr_status(issue_number: str):
    """Get PR status for an issue -- approval state, CI, mergeability."""
    from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

    return await get_pr_status_for_issue(issue_number)


@router.post("/agents/command")
async def run_command(req: RunCommandRequest):
    """Execute a Claude Code command (e.g. /integrate-pr, /approve-merge)."""
    return await control_service.start_command(req.command, req.args or {})


@router.websocket("/ws/agents/status")
async def ws_agent_status(websocket: WebSocket) -> None:
    """Send periodic tick notifications so the client can refresh agent status.

    The client triggers a full ``loadAgents()`` HTTP fetch on each tick,
    so the server only needs to signal "time to refresh" -- no expensive
    status computation here.
    """
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "tick"})
            await asyncio.sleep(2)
    except Exception:
        pass

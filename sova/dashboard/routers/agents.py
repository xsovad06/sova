"""Agents API -- multi-agent start, stop, status, and output streaming."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sova.dashboard.services import control_service
from sova.utils.logging import get_logger

router = APIRouter(tags=["agents"])
log = get_logger(component="dashboard.agents")


class StartAgentRequest(BaseModel):
    issue: str
    role: str | None = None
    force: bool = False


@router.get("/agents/active")
async def get_active_agents():
    """Get all running + recently completed agents."""
    return await control_service.get_all_agents()


@router.get("/agents/interrupted")
async def interrupted_runs():
    """Get recently interrupted runs (from dashboard crash/restart)."""
    from sova.dashboard.services import run_service
    from sova.db.session import get_session

    try:
        session = await get_session()
        async with session.begin():
            runs = await run_service.list_runs(session, status="interrupted", limit=5)
        await session.close()
        return {"interrupted": runs}
    except Exception:
        log.debug("interrupted_runs.failed", exc_info=True)
        return {"interrupted": []}


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
    return await control_service.start_agent(req.issue, role=req.role, force=req.force)


@router.post("/agents/{run_id}/stop")
async def stop_agent(run_id: int):
    """Stop a specific running agent."""
    return await control_service.stop_agent(run_id=run_id)

"""Control API -- start, stop, and monitor agent processes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sova.dashboard.services import control_service

router = APIRouter()


class StartRequest(BaseModel):
    issue: str
    role: str | None = None
    force: bool = False


@router.get("/control/status")
async def agent_status():
    return control_service.get_status()


@router.get("/control/output")
async def agent_output(since: int = 0):
    lines = control_service.get_output(since)
    return {"lines": lines, "total": since + len(lines)}


@router.post("/control/start")
async def start_agent(req: StartRequest):
    return await control_service.start_agent(req.issue, role=req.role, force=req.force)


@router.post("/control/stop")
async def stop_agent():
    return await control_service.stop_agent()


@router.get("/control/interrupted")
async def interrupted_runs():
    """Get recently interrupted runs (from dashboard crash/restart)."""
    from sova.dashboard.services import run_service
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                runs = await run_service.list_runs(session, status="interrupted", limit=5)
            return {"interrupted": runs}
    except Exception:
        return {"interrupted": []}

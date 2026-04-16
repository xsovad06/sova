"""Agent control API — start, stop, status, respond, and log streaming."""

from fastapi import APIRouter
from pydantic import BaseModel
from app.services import process_service

router = APIRouter()


class StartRequest(BaseModel):
    mode: str = "workflow"
    ticket: str | None = None
    args: list[str] | None = None


class RespondRequest(BaseModel):
    id: str
    action: str
    value: str = ""


@router.post("/agent/start")
async def start_agent(req: StartRequest):
    return process_service.start_agent(req.mode, req.ticket, req.args)


@router.post("/agent/stop")
async def stop_agent():
    return process_service.stop_agent()


@router.get("/agent/status")
async def agent_status():
    return process_service.get_status()


@router.get("/agent/request")
async def pending_request():
    req = process_service.get_pending_request()
    if req is None:
        return {"pending": False}
    return {"pending": True, "request": req}


@router.post("/agent/respond")
async def respond(req: RespondRequest):
    return process_service.send_response(req.id, req.action, req.value)


@router.get("/agent/output")
async def agent_output(since: int = 0):
    lines = process_service.get_output(since)
    return {"lines": lines, "total": since + len(lines)}




@router.get("/agent/notifications")
async def agent_notifications(since: int = 0):
    notifs = process_service.get_notifications(since)
    total = since + len(notifs)
    return {"notifications": notifs, "total": total}


# WebSocket endpoint is registered in main.py (outside /api prefix)

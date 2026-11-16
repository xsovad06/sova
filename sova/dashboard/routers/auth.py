"""Auth API: LLM provider/agent runtime auth status and the Connections page."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sova.config.context import resolve_project_dir
from sova.dashboard.security import require_same_origin_csrf
from sova.dashboard.services import setup_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.auth")


def _project_dir() -> Path:
    """The active project: the per-request one, else the single-project startup default, else the cwd."""
    return resolve_project_dir(default_project_dir=get_default_project_dir())


@router.get("/auth/status", responses={503: {"description": "Provider auth status unavailable"}})
async def auth_status() -> dict:
    project_dir = _project_dir()
    try:
        return await setup_service.get_auth_status(project_dir)
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("auth.status.error", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to fetch provider auth status") from exc


@router.get("/connections", responses={503: {"description": "Connection catalog unavailable"}})
async def connections_catalog(request: Request) -> dict:
    """List every supported LLM provider and agent runtime with its readiness state."""
    project_dir = _project_dir()
    is_loopback_bind = bool(getattr(request.app.state, "is_loopback_bind", True))
    try:
        return await setup_service.get_connection_catalog(project_dir, is_loopback_bind=is_loopback_bind)
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("auth.connections.catalog.error", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to fetch connection catalog") from exc


class _LLMCandidateRequest(BaseModel):
    provider: str
    model: str = ""
    api_base: str = ""


class _RuntimeCandidateRequest(BaseModel):
    runtime: str


@router.post(
    "/connections/llm/validate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def validate_llm_connection(req: _LLMCandidateRequest) -> dict:
    """Free readiness probe for a candidate LLM provider. Never persists anything.

    Guarded even though it persists nothing: the probe reaches out to the
    caller-supplied ``api_base`` (the Ollama vendor check issues a real
    ``GET {api_base}/api/tags``) and spawns provider CLI subprocesses, so an
    unguarded version would hand any page in the operator's browser a
    reachability oracle for arbitrary hosts.
    """
    project_dir = _project_dir()
    return await setup_service.validate_llm_candidate(project_dir, req.provider, model=req.model, api_base=req.api_base)


@router.post(
    "/connections/llm/activate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def activate_llm_connection(req: _LLMCandidateRequest) -> dict:
    """Persist a candidate LLM provider as llm.provider, after re-validating it."""
    project_dir = _project_dir()
    return await setup_service.activate_llm_candidate(project_dir, req.provider, model=req.model, api_base=req.api_base)


@router.post(
    "/connections/llm/test",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def test_llm_connection(req: _LLMCandidateRequest) -> dict:
    """Run one real, billable invoke() call against a candidate LLM provider."""
    project_dir = _project_dir()
    return await setup_service.test_llm_candidate(project_dir, req.provider, model=req.model, api_base=req.api_base)


@router.post(
    "/connections/runtime/validate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def validate_runtime_connection(req: _RuntimeCandidateRequest) -> dict:
    """Free readiness probe for a candidate agent runtime. Never persists anything.

    Guarded for the same reason as the LLM probe above: it spawns a runtime
    CLI subprocess rather than only reading state.
    """
    project_dir = _project_dir()
    return await setup_service.validate_runtime_candidate(project_dir, req.runtime)


@router.post(
    "/connections/runtime/activate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def activate_runtime_connection(req: _RuntimeCandidateRequest) -> dict:
    """Persist a candidate agent runtime as agent.runtime, after re-validating it."""
    project_dir = _project_dir()
    return await setup_service.activate_runtime_candidate(project_dir, req.runtime)


def _loopback(request: Request) -> bool:
    return bool(getattr(request.app.state, "is_loopback_bind", True))


_UNKNOWN_FLOW_RESPONSES: dict = {404: {"description": "Unknown reconnect flow"}}


def _known_flow(flow: str) -> str:
    if flow not in setup_service.RECONNECT_FLOWS:
        raise HTTPException(status_code=404, detail=f"Unknown reconnect flow: {flow}")
    return flow


@router.get("/connections/reconnect/availability")
async def reconnect_availability(request: Request) -> dict:
    """Whether each reconnect flow (claude, gh, google) can start from this session, and why not."""
    flows = await setup_service.get_reconnect_flow_availability(_project_dir(), is_loopback_bind=_loopback(request))
    return {"flows": flows}


@router.post(
    "/connections/reconnect/{flow}/start",
    dependencies=[Depends(require_same_origin_csrf)],
    responses=_UNKNOWN_FLOW_RESPONSES,
)
async def start_flow_reconnect(flow: str, request: Request) -> dict:
    """Start the CLI-owned login subprocess for one flow (claude, gh or google)."""
    return await setup_service.start_reconnect(
        _project_dir(), is_loopback_bind=_loopback(request), flow=_known_flow(flow)
    )


@router.get("/connections/reconnect/{flow}/status", responses=_UNKNOWN_FLOW_RESPONSES)
async def flow_reconnect_status(flow: str) -> dict:
    return setup_service.get_reconnect_status(_project_dir(), _known_flow(flow))


@router.post(
    "/connections/reconnect/{flow}/cancel",
    dependencies=[Depends(require_same_origin_csrf)],
    responses=_UNKNOWN_FLOW_RESPONSES,
)
async def cancel_flow_reconnect(flow: str) -> dict:
    return await setup_service.cancel_reconnect(_project_dir(), _known_flow(flow))


# The claude flow keeps its original URLs as aliases of the flow-keyed routes above.
@router.post(
    "/connections/reconnect/start",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def start_reconnect(request: Request) -> dict:
    """Start a CLI-owned `claude auth login` subprocess for local reconnect."""
    return await start_flow_reconnect("claude", request)


@router.get("/connections/reconnect/status")
async def reconnect_status() -> dict:
    return await flow_reconnect_status("claude")


@router.post(
    "/connections/reconnect/cancel",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def cancel_reconnect() -> dict:
    return await cancel_flow_reconnect("claude")

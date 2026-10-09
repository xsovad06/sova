"""Auth API: LLM provider/agent runtime auth status and the Connections page."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sova.config.context import get_project_dir
from sova.dashboard.security import require_same_origin_csrf
from sova.dashboard.services import setup_service
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.auth")


@router.get("/auth/status", responses={503: {"description": "Provider auth status unavailable"}})
async def auth_status() -> dict:
    project_dir = get_project_dir() or Path.cwd()
    try:
        return await setup_service.get_auth_status(project_dir)
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("auth.status.error", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to fetch provider auth status") from exc


@router.get("/connections", responses={503: {"description": "Connection catalog unavailable"}})
async def connections_catalog() -> dict:
    """List every supported LLM provider and agent runtime with its readiness state."""
    project_dir = get_project_dir() or Path.cwd()
    try:
        return await setup_service.get_connection_catalog(project_dir)
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
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.validate_llm_candidate(project_dir, req.provider, model=req.model, api_base=req.api_base)


@router.post(
    "/connections/llm/activate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def activate_llm_connection(req: _LLMCandidateRequest) -> dict:
    """Persist a candidate LLM provider as llm.provider, after re-validating it."""
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.activate_llm_candidate(project_dir, req.provider, model=req.model, api_base=req.api_base)


@router.post(
    "/connections/llm/test",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def test_llm_connection(req: _LLMCandidateRequest) -> dict:
    """Run one real, billable invoke() call against a candidate LLM provider."""
    project_dir = get_project_dir() or Path.cwd()
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
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.validate_runtime_candidate(project_dir, req.runtime)


@router.post(
    "/connections/runtime/activate",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def activate_runtime_connection(req: _RuntimeCandidateRequest) -> dict:
    """Persist a candidate agent runtime as agent.runtime, after re-validating it."""
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.activate_runtime_candidate(project_dir, req.runtime)


@router.post(
    "/connections/reconnect/start",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def start_reconnect() -> dict:
    """Start a CLI-owned `claude auth login` subprocess for local reconnect."""
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.start_reconnect(project_dir)


@router.get("/connections/reconnect/status")
async def reconnect_status() -> dict:
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.get_reconnect_status(project_dir)


@router.post(
    "/connections/reconnect/cancel",
    dependencies=[Depends(require_same_origin_csrf)],
)
async def cancel_reconnect() -> dict:
    project_dir = get_project_dir() or Path.cwd()
    return await setup_service.cancel_reconnect(project_dir)

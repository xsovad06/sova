"""A2A protocol API router: Agent Card discovery and task management."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select

from sova.a2a.agent_card import generate_role_card
from sova.a2a.task_mapping import sova_status_to_a2a, task_run_to_a2a_task
from sova.config.context import resolve_project_dir, resolve_project_dir_or_none
from sova.config.loader import load_config
from sova.config.models import ProjectConfig
from sova.dashboard.services import mcp_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.dashboard.services.mcp_service import validate_mcp_token
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.a2a")

router = APIRouter(tags=["a2a"])

_CONFIG_CACHE_TTL = 60.0
_cached_config = None
_cached_config_dir = None
_cached_config_ts = 0.0


def _get_a2a_config() -> ProjectConfig | None:
    """Load A2A config with a TTL cache to avoid disk reads on every request."""
    global _cached_config, _cached_config_dir, _cached_config_ts

    project_dir = resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    if project_dir is None:
        return None

    now = time.monotonic()
    if (
        _cached_config is not None
        and _cached_config_dir == project_dir
        and (now - _cached_config_ts) < _CONFIG_CACHE_TTL
    ):
        return _cached_config

    _cached_config = load_config(project_dir)
    _cached_config_dir = project_dir
    _cached_config_ts = now
    return _cached_config


def _require_a2a_enabled() -> None:
    """Raise 404 if A2A protocol is disabled in config."""
    cfg = _get_a2a_config()
    if cfg is None or not cfg.a2a.enabled:
        raise HTTPException(status_code=404, detail="A2A protocol is disabled")


def _parse_run_id(task_id: str) -> int | None:
    """Extract the numeric TaskRun ID from an A2A task ID like 'sova-run-42'."""
    prefix = "sova-run-"
    if task_id.startswith(prefix):
        try:
            return int(task_id[len(prefix) :])
        except ValueError:
            return None
    return None


@router.get("/tasks")
async def list_a2a_tasks(request: Request, limit: int = 50, offset: int = 0):
    """List recent SOVA tasks in A2A format with pagination."""
    _require_a2a_enabled()

    capped_limit = min(max(limit, 1), 100)
    capped_offset = max(offset, 0)

    async with await get_session() as session, session.begin():
        count_stmt = select(func.count(TaskRun.id))
        total = (await session.execute(count_stmt)).scalar() or 0

        stmt = select(TaskRun).order_by(TaskRun.started_at.desc()).offset(capped_offset).limit(capped_limit)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        tasks = [task_run_to_a2a_task(r) for r in runs]

    return {"tasks": tasks, "total": total, "limit": capped_limit, "offset": capped_offset}


@router.get("/tasks/{task_id}")
async def get_a2a_task(request: Request, task_id: str):
    """Get a single SOVA task in A2A format."""
    _require_a2a_enabled()

    run_id = _parse_run_id(task_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Invalid task ID: {task_id}")

    async with await get_session() as session, session.begin():
        run = await session.get(TaskRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return task_run_to_a2a_task(run)


def _validate_a2a_cancel_auth(
    task_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> int:
    """Gate the one mutating A2A route with the same run-scoped HMAC token MCP uses.

    Every other A2A route is a read, unauthenticated by design (see the module's
    read-only task/card endpoints above). Cancellation is the exception, so it
    reuses `mcp_service`'s existing generate/validate pair rather than a new
    scheme: a caller must hold a token minted for the exact run_id `task_id`
    encodes, the same bearer-token shape agents already present to `/mcp`.

    Raises:
        HTTPException: 404 if A2A is disabled or the task ID is malformed,
            401 if the token is missing or invalid, 403 if it is valid but
            scoped to a different run.
    """
    _require_a2a_enabled()

    run_id = _parse_run_id(task_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Invalid task ID: {task_id}")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())
    secret = mcp_service.get_or_generate_secret(project_dir)
    token = authorization[7:]

    try:
        token_run_id = validate_mcp_token(token, secret)
    except ValueError as e:
        log.warning("a2a.cancel_auth_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=401, detail=str(e)) from e

    if token_run_id != run_id:
        raise HTTPException(status_code=403, detail=f"Token does not authorize task {task_id}")

    return run_id


@router.post("/tasks/{task_id}/cancel")
async def cancel_a2a_task(
    request: Request,
    task_id: str,
    authenticated_run_id: Annotated[int, Depends(_validate_a2a_cancel_auth)],
) -> dict[str, Any]:
    """Cancel a running SOVA task."""
    from sova.core.state import TASK_RUN_TERMINAL
    from sova.dashboard.services import control_service

    async with await get_session() as session, session.begin():
        run = await session.get(TaskRun, authenticated_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        if run.status in TASK_RUN_TERMINAL:
            a2a_state = sova_status_to_a2a(run.status)
            return {"id": task_id, "status": {"state": a2a_state, "message": "Already terminal"}}
        run.status = "rejected"

    try:
        await control_service.stop_agent(run_id=authenticated_run_id)
    except Exception:  # noqa: BLE001 (the run is already marked rejected; stop failure is reported only)
        log.warning("a2a.stop_agent_after_reject_failed", run_id=authenticated_run_id, exc_info=True)
    return {"id": task_id, "status": {"state": "canceled", "message": "Task canceled"}}


@router.get("/{role}/agent.json")
async def role_agent_card(request: Request, role: str):
    """Return the A2A Agent Card for a specific SOVA role."""
    _require_a2a_enabled()
    cfg = _get_a2a_config()
    endpoint_base = (cfg.a2a.endpoint_base if cfg else "") or str(request.base_url).rstrip("/")
    card = generate_role_card(role, endpoint_base=endpoint_base)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")
    return card

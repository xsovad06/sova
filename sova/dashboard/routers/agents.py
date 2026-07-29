"""Agents API: multi-agent start, stop, status, and output streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import StreamingResponse

from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.services import control_service
from sova.dashboard.services.output_stream_service import get_output_stream_service
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter(tags=["agents"])
log = get_logger(component="dashboard.agents")

_STATUS_BROADCAST_INTERVAL = 2  # seconds


class _ConnectionManager:
    """Track active WebSocket connections per project and run a single producer per group.

    Instead of each connection fetching statuses independently, one background
    task per project_dir polls ``get_all_agent_statuses`` and broadcasts to all
    subscribers in that group.  This keeps backend work constant regardless of
    client count and correctly scopes data in multi-project mode.
    """

    def __init__(self) -> None:
        # Keyed by project_dir (None for single-project mode)
        self._groups: dict[Path | None, list[WebSocket]] = {}
        self._producer_tasks: dict[Path | None, asyncio.Task] = {}

    @property
    def active_connections(self) -> list[WebSocket]:
        return [ws for conns in self._groups.values() for ws in conns]

    def connect(self, ws: WebSocket, project_dir: Path | None = None) -> None:
        group = self._groups.setdefault(project_dir, [])
        group.append(ws)
        task = self._producer_tasks.get(project_dir)
        if task is None or task.done():
            self._producer_tasks[project_dir] = asyncio.create_task(self._produce_loop(project_dir))

    def disconnect(self, ws: WebSocket, project_dir: Path | None = None) -> None:
        group = self._groups.get(project_dir, [])
        try:
            group.remove(ws)
        except ValueError:
            pass
        if not group:
            self._groups.pop(project_dir, None)

    async def cancel_all(self) -> None:
        """Cancel all producer tasks. Called during lifespan shutdown."""
        tasks = [t for t in self._producer_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._producer_tasks.clear()
        self._groups.clear()

    async def broadcast_event(self, event_type: str, project_dir: Path | None) -> None:
        """Send a one-shot event to all subscribers for a project.

        No-op when no connections exist for the project directory.
        """
        if not self._groups.get(project_dir):
            return
        await self._broadcast({"type": event_type}, project_dir)

    async def _broadcast(self, data: dict, project_dir: Path | None) -> None:
        sockets = list(self._groups.get(project_dir, []))
        if not sockets:
            return

        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_json(data), timeout=2.0)
            except Exception:
                return ws
            return None

        results = await asyncio.gather(*(_send(ws) for ws in sockets))
        for failed_ws in results:
            if failed_ws is not None:
                self.disconnect(failed_ws, project_dir)

    async def _produce_loop(self, project_dir: Path | None) -> None:
        """Single producer per project: fetch statuses and broadcast."""
        from sova.dashboard.services.agent_status import (
            format_status_update,
            get_all_agent_statuses,
        )

        try:
            while self._groups.get(project_dir):
                try:
                    statuses = await get_all_agent_statuses(project_dir=project_dir)
                    message = format_status_update(statuses)
                except Exception:
                    log.warning("Failed to fetch agent statuses for WebSocket broadcast", exc_info=True)
                    message = {"type": "status_update", "runs": []}
                await self._broadcast(message, project_dir)
                await asyncio.sleep(_STATUS_BROADCAST_INTERVAL)
        except asyncio.CancelledError:
            raise


_ws_manager = _ConnectionManager()


class StartAgentRequest(BaseModel):
    issue: str = ""
    role: str | None = None
    force: bool = False
    resume_run_id: int | None = None
    pr_number: int | None = None
    model: str | None = None

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

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip().lower()
        if not value:
            return None
        _KNOWN_ALIASES = {"haiku", "sonnet", "opus", "fast", "smart", "cheap"}
        if value not in _KNOWN_ALIASES:
            raise ValueError(f"unknown model alias {value!r}, expected one of: {', '.join(sorted(_KNOWN_ALIASES))}")
        return value


class RunCommandRequest(BaseModel):
    command: str
    args: dict | None = None


class PlannedTaskRequest(BaseModel):
    title: str
    description: str = ""
    labels: list[str] | None = None


class CreateIssuesRequest(BaseModel):
    tasks: list[PlannedTaskRequest] = Field(max_length=50)


@router.post("/agents/planner/create-issues")
async def create_planner_issues(req: CreateIssuesRequest) -> dict:
    """Create GitHub issues from planner-proposed tasks."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    if not req.tasks:
        return {"created": [], "errors": []}

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    adapter = create_adapter(cfg)

    sem = asyncio.Semaphore(4)

    async def _create_one(task: PlannedTaskRequest) -> dict:
        try:
            async with sem:
                result = await adapter.create_issue(
                    title=task.title,
                    body=task.description,
                    labels=task.labels,
                )
            return {"ok": True, "number": result.id, "title": result.title}
        except Exception as exc:
            log.warning("planner.create_issue_failed", title=task.title, error=str(exc), exc_info=True)
            return {"ok": False, "title": task.title, "error": str(exc)}

    results = await asyncio.gather(*[_create_one(t) for t in req.tasks])
    created = [{"number": r["number"], "title": r["title"]} for r in results if r["ok"]]
    errors = [{"title": r["title"], "error": r["error"]} for r in results if not r["ok"]]

    return {"created": created, "errors": errors}


@router.get("/agents/issue/{issue_number}/complexity")
async def get_issue_complexity(issue_number: str) -> dict:
    """Return complexity tier and suggested model for an issue.

    Checks TaskAssessmentRecord first; falls back to on-the-fly assessment
    via ``assess_complexity()`` using issue metadata from the adapter.
    """
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.db.models import TaskAssessmentRecord
    from sova.llm.complexity import ComplexityTier, assess_complexity
    from sova.llm.routing import route_model

    project_dir = get_project_dir()
    cfg = load_config(project_dir)

    # 1. Check DB for existing assessment
    complexity_str: str | None = None
    async with await get_session(project_dir) as session:
        row = (
            await session.execute(
                select(TaskAssessmentRecord.estimated_complexity)
                .where(TaskAssessmentRecord.issue_number == issue_number)
                .order_by(TaskAssessmentRecord.assessed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row:
            complexity_str = row

    # 2. Compute on-the-fly if no DB record
    if complexity_str is None:
        try:
            adapter = create_adapter(cfg)
            task = await adapter.get_task(issue_number)
            tier = assess_complexity(
                task.title,
                task.description or "",
                labels=list(task.labels or []),
            )
            complexity_str = tier.value
        except Exception:
            log.debug("complexity.assess_fallback_failed", issue=issue_number, exc_info=True)
            complexity_str = ComplexityTier.MODERATE.value

    try:
        tier = ComplexityTier(complexity_str)
    except ValueError:
        log.debug("complexity.invalid_db_value", value=complexity_str, issue=issue_number)
        tier = ComplexityTier.MODERATE
    model_alias, reason = route_model(tier, llm_config=cfg.llm)

    # Build model options for the dropdown (config-aware via route_model)
    model_options = []
    for t in ComplexityTier:
        alias, _ = route_model(t, llm_config=cfg.llm)
        model_options.append({"tier": t.value, "model": alias})

    return {
        "complexity": tier.value,
        "suggested_model": model_alias,
        "reason": reason,
        "model_options": model_options,
    }


@router.get("/agents/work-items")
async def get_work_items() -> dict:
    """Get unified work items with computed state and actions."""
    from sova.dashboard.project_context import get_project_dir
    from sova.dashboard.services.work_item_service import get_work_items as _get_work_items

    return await _get_work_items(get_project_dir())


@router.get("/agents/active")
async def get_active_agents() -> dict:
    """Get all running + recently completed agents (dashboard + external)."""
    return await control_service.get_unified_agents()


@router.get("/agents/interrupted")
async def interrupted_runs() -> dict:
    """Get recently interrupted runs (from dashboard crash/restart)."""
    from sova.dashboard.services.agent_recovery import get_interrupted_runs

    runs = await get_interrupted_runs()
    return {"interrupted": runs}


@router.post("/agents/interrupted/dismiss")
async def dismiss_interrupted() -> dict:
    """Mark all interrupted runs as failed so they no longer show in the banner."""
    from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

    count = await dismiss_interrupted_runs()
    return {"dismissed": count}


@router.get("/agents/pipeline")
async def get_pipeline() -> dict:
    """Get the developer pipeline step names."""
    return {"steps": control_service.DEVELOPER_PIPELINE}


@router.get("/agents/kanban")
async def get_kanban(per_column: Annotated[int, Query(ge=1, le=100)] = 10) -> dict[str, Any]:
    """Get non-terminal TaskRuns grouped into Kanban columns."""
    # Config is loaded per-request intentionally: load_config() caches internally,
    # and per-request loading supports hot-reload of sova.toml without restart.
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    mode = cfg.dashboard.kanban_columns

    # Sequential: AsyncSession doesn't support concurrent queries on the same connection.
    async with await get_session() as session:
        columns = await control_service.get_kanban_columns(session, per_column=per_column, mode=mode)
        failed_runs = await control_service.get_recent_failed_runs(session)
    return {"columns": columns, "mode": mode, "failed_runs": failed_runs}


@router.get("/agents/{run_id}/output")
async def get_agent_output(run_id: int, since: int = 0) -> dict:
    """Get output lines for a specific agent."""
    lines = await control_service.get_output(since, run_id=run_id)
    return {"lines": lines, "total": since + len(lines)}


@router.get("/agents/{run_id}/output/stream")
async def stream_agent_output(run_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint: streams output lines for a specific agent run in real time."""
    oss = get_output_stream_service()
    sub_id, queue = oss.subscribe(run_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15.0)
                    escaped = line.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
                    yield f"event: output\ndata: {escaped}\n\n"
                except asyncio.TimeoutError:
                    # Check if the run has reached a terminal state
                    is_done = await _check_run_terminal(run_id)
                    if is_done:
                        yield "event: done\ndata: {}\n\n"
                        break
                    yield ": keepalive\n\n"
        finally:
            oss.unsubscribe(run_id, sub_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _check_run_terminal(run_id: int) -> bool:
    """Check whether a run has reached a terminal status."""
    try:
        async with await get_session() as session:
            row = await session.execute(select(TaskRun.status).where(TaskRun.id == run_id))
            status = row.scalar()
            return status in TASK_RUN_TERMINAL if status else True
    except Exception:
        log.warning("output_stream.terminal_check_failed", run_id=run_id, exc_info=True)
        return False


@router.post("/agents/start")
async def start_agent(req: StartAgentRequest) -> dict:
    """Start a new agent process."""
    if not req.issue and not req.role:
        raise HTTPException(status_code=400, detail="Either issue or role is required for starting an agent")
    result = await control_service.start_agent(
        req.issue,
        role=req.role,
        force=req.force,
        resume_run_id=req.resume_run_id,
        pr_number=req.pr_number,
        model=req.model,
    )
    if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
        raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Command failed")
    return result


@router.post("/agents/{run_id}/stop")
async def stop_agent(run_id: int) -> dict:
    """Stop a specific running agent."""
    return await control_service.stop_agent(run_id=run_id)


@router.post(
    "/agents/{run_id}/resume-from-approval",
    responses={
        404: {"description": "TaskRun not found"},
        409: {"description": "TaskRun not in awaiting_approval state"},
        500: {"description": "Agent spawn or internal error"},
    },
)
async def resume_from_approval(run_id: int) -> dict:
    """Resume a paused pipeline run after human approval."""
    result = await control_service.resume_from_approval(run_id)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail=result["detail"])
    if result.get("error") == "conflict":
        raise HTTPException(status_code=409, detail=result["detail"])
    if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
        raise HTTPException(status_code=500, detail=result.get("detail", result["error"]))
    return result


@router.get("/agents/issue/{issue_number}/pr-status")
async def get_issue_pr_status(issue_number: str) -> dict:
    """Get PR status for an issue -- approval state, CI, mergeability."""
    from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

    return await get_pr_status_for_issue(issue_number)


@router.post("/agents/command")
async def run_command(req: RunCommandRequest) -> dict:
    """Execute a Claude Code command (e.g. /integrate-pr, /address-pr)."""
    result = await control_service.start_command(req.command, req.args or {})
    if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
        raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Command failed")
    return result


@router.websocket("/ws/agents/status")
async def ws_agent_status(websocket: WebSocket) -> None:
    """Push real-time agent status updates over WebSocket.

    The connection registers with ``_ws_manager`` which runs a single
    background producer per project.  That producer fetches statuses once
    per interval and broadcasts ``{type: 'status_update', runs: [...]}``
    to every subscriber, keeping backend work constant regardless of
    client count.
    """
    from sova.dashboard.project_context import get_project_dir

    await websocket.accept()
    # Capture project_dir at connection time so the producer loop
    # uses the correct scope even in multi-project mode.
    project_dir = get_project_dir()
    _ws_manager.connect(websocket, project_dir)
    try:
        # Keep connection alive -- wait for client messages or disconnect.
        # The producer task in _ws_manager handles sending updates.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Unexpected error in WebSocket /ws/agents/status")
    finally:
        _ws_manager.disconnect(websocket, project_dir)

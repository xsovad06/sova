"""Oversight API router: status, runs, findings, run-now trigger, and finding actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sova.oversight.agent import OversightAgent

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.services import oversight_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.oversight")

router = APIRouter(prefix="/oversight", tags=["oversight"])

_oversight_agent: OversightAgent | None = None
_background_tasks: set[asyncio.Task] = set()


def set_oversight_agent(agent: OversightAgent | None) -> None:
    """Called by app.py lifespan to share the oversight agent instance."""
    global _oversight_agent
    _oversight_agent = agent


async def cancel_run_now_tasks() -> None:
    """Cancel any in-flight 'Run Now' background tasks during shutdown."""
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        _background_tasks.clear()


@router.get("/status")
async def get_status() -> dict:
    """Return oversight agent status and summary stats."""
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
    except Exception:
        cfg = None

    enabled = cfg.oversight.enabled if cfg else False
    wake_interval = cfg.oversight.wake_interval_minutes if cfg else 60
    agent_running = _oversight_agent is not None and _oversight_agent.running

    try:
        async with await get_session() as session:
            return await oversight_service.get_status(
                session,
                enabled=enabled,
                agent_running=agent_running,
                wake_interval_minutes=wake_interval,
            )
    except Exception:
        log.warning("oversight.status.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch oversight status")


@router.get("/runs")
async def get_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Return recent oversight run records."""
    try:
        async with await get_session() as session:
            runs = await oversight_service.get_runs(session, limit=limit)
            return {"runs": runs}
    except Exception:
        log.warning("oversight.runs.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch oversight runs")


@router.get("/findings")
async def get_findings(
    status: str = Query(default="pending", pattern="^(pending|created|dismissed)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Return oversight findings filtered by status."""
    try:
        async with await get_session() as session:
            findings = await oversight_service.get_findings(session, status=status, limit=limit)
            return {"findings": findings}
    except Exception:
        log.warning("oversight.findings.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch oversight findings")


@router.post(
    "/run-now",
    status_code=202,
    responses={
        404: {"description": "Oversight agent is not running"},
        409: {"description": "A manual cycle is already pending"},
    },
)
async def trigger_run_now() -> dict:
    """Trigger an immediate oversight cycle (fire-and-forget)."""
    if _oversight_agent is None or not _oversight_agent.running:
        raise HTTPException(status_code=404, detail="Oversight agent is not running")
    if _background_tasks:
        raise HTTPException(status_code=409, detail="A manual cycle is already pending")
    task = asyncio.create_task(_oversight_agent.run_cycle_once())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "accepted"}


@router.post(
    "/findings/{finding_id}/create-issue",
    responses={
        404: {"description": "Finding not found"},
        409: {"description": "Finding already has a linked issue"},
    },
)
async def create_issue_from_finding(finding_id: int) -> dict:
    """Create a GitHub Issue from a pending finding."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.oversight.actions import _issue_body, _issue_labels

    try:
        async with await get_session() as session:
            async with session.begin():
                finding = await oversight_service.get_finding_by_id(session, finding_id)
                if finding is None:
                    raise HTTPException(status_code=404, detail="Finding not found")
                if finding.dismissed:
                    raise HTTPException(status_code=409, detail="Finding is dismissed")
                if finding.github_issue_number is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Finding already linked to issue #{finding.github_issue_number}",
                    )

                project_dir = get_project_dir()
                cfg = load_config(project_dir)
                adapter = create_adapter(cfg)

                task = await adapter.create_issue(
                    title=finding.title,
                    body=_issue_body(finding),
                    labels=_issue_labels(finding),
                )
                issue_number = int(task.id)
                await oversight_service.update_finding_issue_number(session, finding_id, issue_number)

                return {"issue_number": issue_number, "finding_id": finding_id}
    except HTTPException:
        raise
    except Exception:
        log.warning("oversight.create_issue.error", finding_id=finding_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create issue from finding")


@router.post(
    "/findings/{finding_id}/dismiss",
    responses={404: {"description": "Finding not found"}},
)
async def dismiss_finding(finding_id: int) -> dict:
    """Mark a finding as dismissed so it won't reappear."""
    try:
        async with await get_session() as session:
            async with session.begin():
                result = await oversight_service.dismiss_finding(session, finding_id)
                if result is None:
                    raise HTTPException(status_code=404, detail="Finding not found")
                return {"finding_id": finding_id, "dismissed": True}
    except HTTPException:
        raise
    except Exception:
        log.warning("oversight.dismiss.error", finding_id=finding_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to dismiss finding")

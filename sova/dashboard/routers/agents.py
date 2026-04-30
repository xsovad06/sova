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
    resume_run_id: int | None = None
    pr_number: int | None = None


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


@router.post("/agents/interrupted/dismiss")
async def dismiss_interrupted():
    """Mark all interrupted runs as failed so they no longer show in the banner."""
    from sqlalchemy import update

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        session = await get_session()
        async with session.begin():
            stmt = (
                update(TaskRun)
                .where(TaskRun.status == "interrupted")
                .values(status="failed", error_message="Dismissed by user")
            )
            result = await session.execute(stmt)
            count = result.rowcount
        await session.close()
        return {"dismissed": count}
    except Exception:
        log.debug("dismiss_interrupted.failed", exc_info=True)
        return {"dismissed": 0}


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
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.git.operations import (
        CheckConclusion,
        CheckStatus,
        find_pr_for_issue,
        get_ci_checks,
        get_pr_status,
    )

    project_dir = get_project_dir()
    if not project_dir:
        return {"has_pr": False}

    try:
        cfg = load_config(project_dir)
    except Exception:
        return {"has_pr": False}

    repo = cfg.github_repo
    gh_user = cfg.github_user
    if not repo:
        return {"has_pr": False}

    pr_info = await find_pr_for_issue(issue_number, repo=repo, github_user=gh_user)
    if not pr_info:
        return {"has_pr": False}

    try:
        status = await get_pr_status(pr_info.number, repo=repo, github_user=gh_user)
    except Exception:
        log.debug("pr_status.fetch_failed", issue=issue_number, exc_info=True)
        return {"has_pr": True, "pr_number": pr_info.number, "error": "Failed to fetch PR status"}

    ci_summary = "unknown"
    try:
        checks = await get_ci_checks(pr_info.number, repo=repo, github_user=gh_user)
        if not checks:
            ci_summary = "none"
        elif all(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.SUCCESS for c in checks):
            ci_summary = "passed"
        elif any(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.FAILURE for c in checks):
            ci_summary = "failed"
        elif any(c.status == CheckStatus.IN_PROGRESS for c in checks):
            ci_summary = "pending"
        else:
            ci_summary = "passed"
    except Exception:
        log.debug("ci_checks.fetch_failed", pr=pr_info.number, exc_info=True)

    return {
        "has_pr": True,
        "pr_number": status.number,
        "state": status.state,
        "review_decision": status.review_decision,
        "mergeable": status.mergeable,
        "ci_status": ci_summary,
        "title": status.title,
        "url": status.url,
        "is_approved": status.is_approved,
        "is_mergeable": status.is_mergeable,
    }


@router.post("/agents/command")
async def run_command(req: RunCommandRequest):
    """Execute a Claude Code command (e.g. /integrate-pr, /approve-merge)."""
    return await control_service.start_command(req.command, req.args or {})

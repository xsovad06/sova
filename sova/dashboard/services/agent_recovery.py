"""Stale run detection, PID liveness checks, and interrupted run management."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control.recovery")


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale 'running' TaskRuns on dashboard startup."""
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        interrupted = []

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.status == "running")
                result = await session.execute(stmt)
                stale_runs = result.scalars().all()

                for run in stale_runs:
                    if run.pid and _is_process_alive(run.pid):
                        log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                        continue

                    run.status = "interrupted"
                    run.error_message = "Dashboard restarted while agent was running"
                    run.ended_at = datetime.now(timezone.utc)
                    interrupted.append(
                        {
                            "run_id": run.id,
                            "issue": run.issue_number,
                            "role": run.role,
                            "pid": run.pid,
                        }
                    )
                    log.warning("recovery.interrupted", run_id=run.id, issue=run.issue_number, pid=run.pid)

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))
        return interrupted
    except Exception:
        log.warning("recovery.failed", exc_info=True)
        return []


async def get_interrupted_runs(limit: int = 5) -> list[dict]:
    """Get recently interrupted task runs."""
    from sova.dashboard.services import run_service
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                return await run_service.list_runs(session, status="interrupted", limit=limit)
    except Exception:
        log.debug("interrupted_runs.query_failed", exc_info=True)
        return []


async def dismiss_interrupted_runs() -> int:
    """Mark all interrupted runs as failed. Returns count of dismissed runs."""
    from sqlalchemy import update

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                stmt = (
                    update(TaskRun)
                    .where(TaskRun.status == "interrupted")
                    .values(status="failed", error_message="Dismissed by user")
                )
                result = await session.execute(stmt)
                return result.rowcount
    except Exception:
        log.debug("dismiss_interrupted.failed", exc_info=True)
        return 0


async def get_pr_status_for_issue(issue_number: str) -> dict:
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

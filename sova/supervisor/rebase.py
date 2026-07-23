"""Supervisor-level auto-rebase for PRs with merge conflicts.

Uses the existing rebase_with_conflict_resolution infrastructure to resolve
conflicts via LLM. Creates a TaskRun for dashboard visibility, validates via
pre-push hook, and pushes with --force-with-lease on success. On failure,
writes a manual-only DashboardHandoff for human intervention.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.loader import load_config
from sova.db.models import TaskRun
from sova.git.pr import find_pr_for_issue
from sova.git.rebase import rebase_with_conflict_resolution
from sova.git.worktree import cleanup_worktree, create_worktree
from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="supervisor.rebase")

ROLE_SUPERVISOR_REBASE = "supervisor:rebase"


async def attempt_auto_rebase(
    issue_number: int,
    project_dir: Path,
    session_factory: async_sessionmaker,
) -> dict[str, Any]:
    """Attempt to auto-rebase a PR branch onto the base branch."""
    worktree_path: Path | None = None
    task_run: TaskRun | None = None
    pr_number: int | None = None
    head_sha: str = ""
    try:
        cfg = load_config(project_dir)
        if not cfg.github_repo:
            return {"status": "skipped", "reason": "No github_repo configured"}

        pr_info = await _get_pr_info(issue_number, cfg.github_repo, cfg.github_user)
        if pr_info is None:
            return {"status": "skipped", "reason": f"No open PR found for issue #{issue_number}"}

        pr_number = pr_info["number"]
        branch = pr_info["branch"]
        head_sha = pr_info["head_sha"]
        base_branch = pr_info.get("base_branch", cfg.base_branch)

        if await _already_attempted(pr_number, head_sha, session_factory):
            return {
                "status": "skipped",
                "reason": f"Already attempted rebase for PR #{pr_number} at HEAD {head_sha[:8]}",
            }

        task_run = await _create_rebase_run(issue_number, pr_number, branch, session_factory)

        worktree_id = f"rebase-pr-{pr_number}"
        worktree_info = await create_worktree(
            issue_id=worktree_id,
            branch=branch,
            base_branch=base_branch,
            project_dir=project_dir,
            copy_files=cfg.worktree.copy_files,
        )
        worktree_path = worktree_info.path

        result, cost = await rebase_with_conflict_resolution(
            base_branch,
            cwd=worktree_path,
            max_commits=5,
        )

        await _update_run_cost(task_run.id, cost, session_factory)

        if not result.success:
            log.warning("auto_rebase.failed", issue=issue_number, pr=pr_number, error=result.error)
            await _finalize_run(task_run.id, "failed", result.error, head_sha, session_factory)
            _write_manual_handoff(project_dir, issue_number, pr_number, branch, f"Auto-rebase failed: {result.error}")
            return {"status": "failed", "pr_number": pr_number, "error": result.error}

        hook_result = await _run_pre_push_hook(worktree_path)
        if not hook_result["passed"]:
            log.warning(
                "auto_rebase.pre_push_failed", issue=issue_number, pr=pr_number, output=hook_result["output"][:500]
            )
            await _finalize_run(task_run.id, "failed", "Pre-push hook failed", head_sha, session_factory)
            _write_manual_handoff(
                project_dir,
                issue_number,
                pr_number,
                branch,
                "Rebase succeeded but pre-push hook failed",
                validation_error=hook_result["output"],
            )
            return {"status": "failed", "pr_number": pr_number, "error": "Pre-push hook failed after rebase"}

        push_result = await run("git", "push", "origin", branch, "--force-with-lease", cwd=worktree_path)

        if not push_result.success:
            if "stale info" in push_result.stderr or "rejected" in push_result.stderr:
                log.info("auto_rebase.branch_changed", issue=issue_number, pr=pr_number)
                await _finalize_run(task_run.id, "done", "Branch HEAD changed during rebase", head_sha, session_factory)
                return {"status": "skipped", "pr_number": pr_number, "reason": "Branch HEAD changed during rebase"}
            await _finalize_run(task_run.id, "failed", push_result.stderr[:500], head_sha, session_factory)
            return {"status": "failed", "pr_number": pr_number, "error": push_result.stderr[:500]}

        log.info("auto_rebase.success", issue=issue_number, pr=pr_number, conflicts_resolved=result.conflicts_resolved)
        await _finalize_run(task_run.id, "done", None, head_sha, session_factory)
        return {"status": "success", "pr_number": pr_number, "conflicts_resolved": result.conflicts_resolved}

    except Exception as exc:
        log.exception("auto_rebase.unexpected_error", issue=issue_number, pr=pr_number)
        if task_run is not None:
            await _finalize_run(task_run.id, "failed", str(exc), head_sha, session_factory)
        return {"status": "failed", "pr_number": pr_number, "error": str(exc)}

    finally:
        if worktree_path is not None:
            try:
                await cleanup_worktree(worktree_path, cwd=project_dir)
            except Exception:
                log.debug("auto_rebase.worktree_cleanup_failed", path=str(worktree_path), exc_info=True)


async def _get_pr_info(issue_number: int, repo: str, github_user: str) -> dict[str, Any] | None:
    """Fetch PR number, branch, and HEAD SHA for an issue."""
    pr = await find_pr_for_issue(str(issue_number), repo=repo, github_user=github_user)
    if pr is None:
        return None

    head_result = await run(
        "gh",
        "pr",
        "view",
        str(pr.number),
        "--repo",
        repo,
        "--json",
        "headRefOid,headRefName,baseRefName",
    )
    if not head_result.success:
        return None

    try:
        data = json.loads(head_result.stdout)
    except (ValueError, KeyError):
        return None

    return {
        "number": pr.number,
        "branch": data.get("headRefName", ""),
        "head_sha": data.get("headRefOid", ""),
        "base_branch": data.get("baseRefName", ""),
    }


async def _already_attempted(pr_number: int, head_sha: str, session_factory: async_sessionmaker) -> bool:
    """Check if we already attempted a rebase for this PR at this HEAD SHA."""
    async with session_factory() as session:
        stmt = select(TaskRun).where(TaskRun.role == ROLE_SUPERVISOR_REBASE, TaskRun.pr_number == pr_number)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        for r in runs:
            if r.handoff_json and r.handoff_json.get("head_sha") == head_sha:
                return True
    return False


async def _create_rebase_run(
    issue_number: int,
    pr_number: int,
    branch: str,
    session_factory: async_sessionmaker,
) -> TaskRun:
    """Create a TaskRun to track the rebase attempt."""
    async with session_factory() as session:
        task_run = TaskRun(
            issue_number=str(issue_number),
            role=ROLE_SUPERVISOR_REBASE,
            status="running",
            current_step="rebase",
            branch_name=branch,
            pr_number=pr_number,
            run_label=f"Auto-rebase PR #{pr_number}",
        )
        session.add(task_run)
        await session.commit()
        await session.refresh(task_run)
        return task_run


async def _update_run_cost(run_id: int, cost: Decimal, session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        task_run = await session.get(TaskRun, run_id)
        if task_run:
            task_run.total_cost_usd += cost
            await session.commit()


async def _finalize_run(
    run_id: int,
    status: str,
    error: str | None,
    head_sha: str,
    session_factory: async_sessionmaker,
) -> None:
    async with session_factory() as session:
        task_run = await session.get(TaskRun, run_id)
        if task_run:
            task_run.status = status
            task_run.error_message = error
            task_run.ended_at = datetime.now(timezone.utc)
            task_run.handoff_json = {"head_sha": head_sha}
            await session.commit()


async def _run_pre_push_hook(worktree_path: Path) -> dict[str, Any]:
    """Run the pre-push hook in the worktree. Returns {passed, output}."""
    hook_path = worktree_path / ".githooks" / "pre-push"
    if not hook_path.exists():
        return {"passed": True, "output": ""}

    result = await run(str(hook_path), cwd=worktree_path, timeout=300)
    return {"passed": result.success, "output": (result.stdout or "") + (result.stderr or "")}


def _write_manual_handoff(
    project_dir: Path,
    issue_number: int,
    pr_number: int,
    branch: str,
    summary: str,
    validation_error: str = "",
) -> None:
    """Write a manual-only handoff for human intervention."""
    details: dict[str, Any] = {}
    if validation_error:
        details["validation_error"] = validation_error

    handoff = DashboardHandoff(
        source=ROLE_SUPERVISOR_REBASE,
        status="failed",
        issue=str(issue_number),
        pr_number=pr_number,
        branch=branch,
        summary=summary,
        details=details,
        next_actions=[
            HandoffAction(
                id="manual_rebase",
                label="Rebase Manually",
                description="Resolve merge conflicts manually and push",
                style="neutral",
                mode="dashboard-only",
            ),
        ],
    )
    write_handoff_file(project_dir, handoff)

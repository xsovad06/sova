"""Supervisor-level auto-rebase for PRs with merge conflicts.

Uses the existing rebase_with_conflict_resolution infrastructure to resolve
conflicts via LLM. Creates a TaskRun for dashboard visibility, validates via
pre-push hook, and pushes with --force-with-lease on success. On failure,
writes a manual-only DashboardHandoff for human intervention.
"""

from __future__ import annotations

import asyncio
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
from sova.git.worktree import check_worktree_active_agent, cleanup_worktree, create_worktree, find_worktree_by_branch
from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="supervisor.rebase")

ROLE_SUPERVISOR_REBASE = "supervisor:rebase"

# Serializes attempt_auto_rebase() calls for the same PR across callers (the
# dashboard's trigger_rebase endpoint and the progression daemon's
# SPAWN_REBASE path both call it directly, with no other coordination), so
# two concurrent calls can't both pass the worktree-reuse checks and race in
# _run_rebase_and_push. Never cleared: entries are one tiny Lock per PR ever
# rebased, matching the identity-keyed tracker pattern in github_quota.py.
_rebase_locks: dict[int, asyncio.Lock] = {}


def _get_rebase_lock(pr_number: int) -> asyncio.Lock:
    lock = _rebase_locks.get(pr_number)
    if lock is None:
        lock = asyncio.Lock()
        _rebase_locks[pr_number] = lock
    return lock


async def attempt_auto_rebase(
    issue_number: int,
    project_dir: Path,
    session_factory: async_sessionmaker,
) -> dict[str, Any]:
    """Attempt to auto-rebase a PR branch onto the base branch."""
    try:
        cfg = load_config(project_dir)
        if not cfg.github_repo:
            return {"status": "skipped", "reason": "No github_repo configured"}

        pr_info = await _get_pr_info(issue_number, cfg.github_repo, cfg.github_user)
        if pr_info is None:
            return {"status": "skipped", "reason": f"No open PR found for issue #{issue_number}"}
    except Exception as exc:  # noqa: BLE001 (no TaskRun exists yet; nothing to finalize)
        log.exception("auto_rebase.setup_error", issue=issue_number)
        return {"status": "failed", "pr_number": None, "error": str(exc)}

    pr_number = pr_info["number"]
    branch = pr_info["branch"]
    head_sha = pr_info["head_sha"]
    base_branch = pr_info.get("base_branch", cfg.base_branch)

    # Finalization and worktree cleanup happen inside this lock (in
    # _run_locked_rebase's own try/except/finally), not after it: releasing
    # the lock before a failed or cancelled run finishes cleaning up its
    # dedicated worktree would let a waiting same-PR call reuse that worktree
    # while the first call is still tearing it down.
    async with _get_rebase_lock(pr_number):
        return await _run_locked_rebase(
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            head_sha=head_sha,
            project_dir=project_dir,
            cfg=cfg,
            session_factory=session_factory,
        )


async def _run_locked_rebase(
    *,
    issue_number: int,
    pr_number: int,
    branch: str,
    base_branch: str,
    head_sha: str,
    project_dir: Path,
    cfg: Any,
    session_factory: async_sessionmaker,
) -> dict[str, Any]:
    """Run the already-attempted check through the rebase-and-push flow.

    Must only be called while holding `_get_rebase_lock(pr_number)`: see the
    comment at the call site in `attempt_auto_rebase()`.
    """
    worktree_path: Path | None = None
    reused_worktree = False
    task_run: TaskRun | None = None
    try:
        if await _already_attempted(pr_number, head_sha, session_factory):
            return {
                "status": "skipped",
                "reason": f"Already attempted rebase for PR #{pr_number} at HEAD {head_sha[:8]}",
            }

        task_run = await _create_rebase_run(issue_number, pr_number, branch, session_factory)

        resolution = await _resolve_rebase_worktree(
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            project_dir=project_dir,
            cfg=cfg,
            task_run=task_run,
            head_sha=head_sha,
            session_factory=session_factory,
        )
        if isinstance(resolution, dict):
            return resolution
        worktree_path, reused_worktree = resolution

        return await _run_rebase_and_push(
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            project_dir=project_dir,
            worktree_path=worktree_path,
            task_run=task_run,
            head_sha=head_sha,
            session_factory=session_factory,
        )

    except asyncio.CancelledError:
        log.info("auto_rebase.cancelled", issue=issue_number, pr=pr_number)
        if task_run is not None:
            await _finalize_run(task_run.id, "failed", "Rebase cancelled", head_sha, session_factory)
        raise

    except Exception as exc:  # noqa: BLE001 (rebase spans git, LLM and DB; any failure is recorded on the run)
        log.exception("auto_rebase.unexpected_error", issue=issue_number, pr=pr_number)
        if task_run is not None:
            await _finalize_run(task_run.id, "failed", str(exc), head_sha, session_factory)
        return {"status": "failed", "pr_number": pr_number, "error": str(exc)}

    finally:
        if worktree_path is not None and not reused_worktree:
            try:
                await cleanup_worktree(worktree_path, cwd=project_dir)
            except (RuntimeError, OSError):
                log.debug("auto_rebase.worktree_cleanup_failed", path=str(worktree_path), exc_info=True)


async def _resolve_rebase_worktree(
    *,
    issue_number: int,
    pr_number: int,
    branch: str,
    base_branch: str,
    project_dir: Path,
    cfg: Any,
    task_run: TaskRun,
    head_sha: str,
    session_factory: async_sessionmaker,
) -> tuple[Path, bool] | dict[str, Any]:
    """Find or create the worktree to rebase *branch* in.

    Returns ``(worktree_path, reused)`` when the caller should continue, or an
    already-finalized result dict when the caller should return early (the
    worktree is busy, or the safety check itself failed).
    """
    prune_result = await run("git", "worktree", "prune", cwd=project_dir)
    if not prune_result.success:
        log.warning("auto_rebase.prune_failed", stderr=prune_result.stderr[:200])

    existing_wt: Path | None = None
    try:
        existing_wt = await find_worktree_by_branch(branch, cwd=project_dir)
    except (RuntimeError, OSError):
        log.debug("auto_rebase.branch_worktree_lookup_failed", branch=branch, exc_info=True)

    if existing_wt is not None and existing_wt.resolve() == project_dir.resolve():
        # The branch is checked out in the main project checkout itself, not a
        # dedicated worktree. create_worktree() would fail the same way this
        # PR fixes (git refuses a second checkout of the same branch), and
        # reusing project_dir directly would risk the main checkout the way
        # /address-pr's own worktree-safety rule warns against. Skip cleanly.
        log.info("auto_rebase.branch_in_main_worktree", issue=issue_number, pr=pr_number)
        reason = f"Branch {branch} is checked out in the main project checkout; auto-rebase does not reuse it"
        await _finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    if existing_wt is None:
        worktree_id = f"rebase-pr-{pr_number}"
        worktree_info = await create_worktree(
            issue_id=worktree_id,
            branch=branch,
            base_branch=base_branch,
            project_dir=project_dir,
            copy_files=cfg.worktree.copy_files,
        )
        return worktree_info.path, False

    # The branch is already checked out elsewhere (e.g. the developer's
    # persistent worktree). Git refuses to check out the same branch into a
    # second worktree, so reuse it instead of colliding.
    try:
        active_pid = await check_worktree_active_agent(existing_wt, project_dir=project_dir)
    except RuntimeError as exc:
        log.warning(
            "auto_rebase.active_agent_check_failed",
            issue=issue_number,
            pr=pr_number,
            error=str(exc),
            exc_info=True,
        )
        reason = f"Cannot verify worktree safety: {exc}"
        await _finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "failed", "pr_number": pr_number, "error": str(exc)}

    if active_pid is not None:
        log.info("auto_rebase.worktree_busy", issue=issue_number, pr=pr_number, pid=active_pid)
        reason = f"Branch {branch} worktree is actively in use by agent PID {active_pid}"
        await _finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    if not await _worktree_is_clean(existing_wt):
        # rebase_with_conflict_resolution() stashes and pops uncommitted
        # changes around the rebase; reusing a dirty worktree risks stranding
        # the developer's in-progress edits in the repo-wide stash if the
        # pop ever fails. Fail closed rather than risk that.
        log.warning("auto_rebase.worktree_dirty_or_unknown", issue=issue_number, pr=pr_number, path=str(existing_wt))
        reason = f"Branch {branch} worktree has uncommitted changes or its status could not be verified"
        await _finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    log.info("auto_rebase.reusing_worktree", issue=issue_number, pr=pr_number, path=str(existing_wt))
    return existing_wt, True


async def _worktree_is_clean(worktree_path: Path) -> bool:
    """Check whether *worktree_path* has no uncommitted changes.

    Returns False when the status can't be read, so an unreadable worktree
    is never reused: `rebase_with_conflict_resolution()` stashes changes on a
    dirty tree, and reuse must fail closed rather than gamble on a clean one.
    """
    status_result = await run("git", "status", "--porcelain", cwd=worktree_path)
    if not status_result.success:
        log.warning(
            "auto_rebase.worktree_status_check_failed",
            path=str(worktree_path),
            stderr=status_result.stderr[:200],
        )
        return False
    return status_result.stdout.strip() == ""


async def _run_rebase_and_push(
    *,
    issue_number: int,
    pr_number: int,
    branch: str,
    base_branch: str,
    project_dir: Path,
    worktree_path: Path,
    task_run: TaskRun,
    head_sha: str,
    session_factory: async_sessionmaker,
) -> dict[str, Any]:
    """Run the rebase, validate via pre-push hook, and push. Always returns a result dict."""
    result, cost = await rebase_with_conflict_resolution(base_branch, cwd=worktree_path, max_commits=5)
    await _update_run_cost(task_run.id, cost, session_factory)

    if not result.success:
        log.warning("auto_rebase.failed", issue=issue_number, pr=pr_number, error=result.error)
        await _finalize_run(task_run.id, "failed", result.error, head_sha, session_factory)
        _write_manual_handoff(project_dir, issue_number, pr_number, branch, f"Auto-rebase failed: {result.error}")
        return {"status": "failed", "pr_number": pr_number, "error": result.error}

    hook_result = await _run_pre_push_hook(worktree_path)
    if not hook_result["passed"]:
        log.warning("auto_rebase.pre_push_failed", issue=issue_number, pr=pr_number, output=hook_result["output"][:500])
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
    """Check if we already attempted a rebase for this PR at this HEAD SHA.

    Ignores safety skips (busy worktree, dirty-or-unreadable worktree, a
    failed safety check, or the branch sitting in the main checkout): those
    are transient conditions that can clear before the next poll, and
    counting them as an attempt would strand the PR until a new commit
    changes head_sha, since none of them ever ran the actual rebase.
    """
    async with session_factory() as session:
        stmt = select(TaskRun).where(TaskRun.role == ROLE_SUPERVISOR_REBASE, TaskRun.pr_number == pr_number)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        for r in runs:
            if not r.handoff_json or r.handoff_json.get("head_sha") != head_sha:
                continue
            if r.handoff_json.get("attempted", True):
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
    *,
    attempted: bool = True,
) -> None:
    """Finalize the run's DB status.

    *attempted* controls whether `_already_attempted()` treats this run as a
    real rebase attempt (default) or a safety skip that a later, transient
    condition change should be allowed to retry.
    """
    async with session_factory() as session:
        task_run = await session.get(TaskRun, run_id)
        if task_run:
            task_run.status = status
            task_run.error_message = error
            task_run.ended_at = datetime.now(timezone.utc)
            task_run.handoff_json = {"head_sha": head_sha, "attempted": attempted}
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

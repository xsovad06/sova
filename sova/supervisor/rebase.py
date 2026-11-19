"""Supervisor-level auto-rebase for PRs with merge conflicts.

Uses the existing rebase_with_conflict_resolution infrastructure to resolve
conflicts via LLM. Creates a TaskRun for dashboard visibility, validates via
pre-push hook, and pushes with --force-with-lease on success. On failure,
writes a manual-only DashboardHandoff for human intervention.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.loader import load_config
from sova.core.state import TaskStatus
from sova.db.models import TaskRun
from sova.git.pr import PRLookupError, find_pr_for_issue_checked
from sova.git.rebase import rebase_with_conflict_resolution
from sova.git.worktree import check_worktree_active_agent, cleanup_worktree, create_worktree, find_worktree_by_branch
from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file
from sova.utils.env import scrub_check_env
from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="supervisor.rebase")

ROLE_SUPERVISOR_REBASE = "supervisor:rebase"

# Bounds on one rebase run, used both to run it and to age a `running` row: a row younger than the bound
# is a live run in another process (the supervisor daemon and the dashboard each hold their own asyncio
# lock), an older one is treated as orphaned by a crash so a manual retry can still go through.
REBASE_MAX_COMMITS = 5
PRE_PUSH_HOOK_TIMEOUT = 300
DEFAULT_RESOLUTION_TIMEOUT = 900.0
RUNNING_REBASE_MARGIN = timedelta(minutes=10)


def running_rebase_stale_after(cfg: Any) -> timedelta:
    """How long a `running` rebase row counts as live: the worst case of one run plus a margin.

    One run resolves up to `REBASE_MAX_COMMITS` commits, each bounded by `llm.cli_timeout` (the timeout
    `_load_consensus_config()` hands the resolver), then runs the pre-push hook.
    """
    try:
        per_commit = float(cfg.llm.cli_timeout) or DEFAULT_RESOLUTION_TIMEOUT
    except (AttributeError, TypeError, ValueError):
        per_commit = DEFAULT_RESOLUTION_TIMEOUT
    return timedelta(seconds=REBASE_MAX_COMMITS * per_commit + PRE_PUSH_HOOK_TIMEOUT) + RUNNING_REBASE_MARGIN


@dataclass
class RebaseOperations:
    """The collaborators attempt_auto_rebase() calls through.

    Tests construct one instance with mocked attributes instead of patching
    each module-level function individually (issue #1209): the orchestration
    logic in this module only ever needs to know the shape of these calls,
    not which concrete function backs them.
    """

    load_config: Callable[..., Any]
    get_pr_info: Callable[..., Any]
    already_attempted: Callable[..., Any]
    create_worktree: Callable[..., Any]
    find_worktree_by_branch: Callable[..., Any]
    check_worktree_active_agent: Callable[..., Any]
    cleanup_worktree: Callable[..., Any]
    rebase_with_conflict_resolution: Callable[..., Any]
    create_rebase_run: Callable[..., Any]
    find_running_rival: Callable[..., Any]
    update_run_cost: Callable[..., Any]
    finalize_run: Callable[..., Any]
    run_pre_push_hook: Callable[..., Any]
    run: Callable[..., Any]
    write_manual_handoff: Callable[..., Any]


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
    *,
    retry_failed: bool = False,
    ops: RebaseOperations | None = None,
) -> dict[str, Any]:
    """Attempt to auto-rebase a PR branch onto the base branch.

    A user-triggered retry may revisit a failed attempt at the same HEAD;
    scheduler calls keep the default deduplication to avoid repeated failures.
    """
    ops = ops if ops is not None else _default_ops()
    try:
        cfg = ops.load_config(project_dir)
        if not cfg.github_repo:
            return {"status": "skipped", "reason": "No github_repo configured"}

        pr_info = await ops.get_pr_info(issue_number, cfg.github_repo, cfg.github_user)
        if pr_info is None:
            return {"status": "skipped", "reason": f"No open PR found for issue #{issue_number}"}
    except PRLookupError as exc:
        log.warning("auto_rebase.pr_lookup_failed", issue=issue_number, error=str(exc))
        return {"status": "failed", "pr_number": None, "error": f"PR lookup failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 (no TaskRun exists yet; nothing to finalize)
        log.exception("auto_rebase.setup_error", issue=issue_number)
        return {"status": "failed", "pr_number": None, "error": str(exc)}

    pr_number = pr_info["number"]
    branch = pr_info["branch"]
    head_sha = pr_info["head_sha"]
    base_branch = pr_info.get("base_branch") or cfg.base_branch

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
            retry_failed=retry_failed,
            ops=ops,
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
    retry_failed: bool,
    ops: RebaseOperations,
) -> dict[str, Any]:
    """Run the already-attempted check through the rebase-and-push flow.

    Must only be called while holding `_get_rebase_lock(pr_number)`: see the
    comment at the call site in `attempt_auto_rebase()`.
    """
    worktree_path: Path | None = None
    reused_worktree = False
    task_run: TaskRun | None = None
    try:
        if await ops.already_attempted(pr_number, head_sha, session_factory, retry_failed=retry_failed):
            return {
                "status": "skipped",
                "reason": f"Already attempted rebase for PR #{pr_number} at HEAD {head_sha[:8]}",
            }

        # Claim first, then look for a rival: the asyncio lock only serializes one process, so a check
        # followed by an insert would let the daemon and a dashboard click both pass it. With the row
        # already written, of any two overlapping runs the one with the higher id sees the other and backs off.
        task_run = await ops.create_rebase_run(issue_number, pr_number, branch, session_factory)
        rival = await ops.find_running_rival(
            task_run.id, pr_number, session_factory, stale_after=running_rebase_stale_after(cfg)
        )
        if rival is not None:
            reason = f"Rebase for PR #{pr_number} is already running{_started_at_suffix(rival.started_at)}"
            await ops.finalize_run(task_run.id, TaskStatus.FAILED, reason, head_sha, session_factory, attempted=False)
            return {"status": "skipped", "pr_number": pr_number, "reason": reason}

        # A rival that finalized between the first check and the claim is no longer running, so the rival
        # check cannot see it. Re-run the attempted check now that the claim is written.
        if await ops.already_attempted(pr_number, head_sha, session_factory, retry_failed=retry_failed):
            reason = f"Already attempted rebase for PR #{pr_number} at HEAD {head_sha[:8]}"
            await ops.finalize_run(task_run.id, TaskStatus.FAILED, reason, head_sha, session_factory, attempted=False)
            return {"status": "skipped", "pr_number": pr_number, "reason": reason}

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
            ops=ops,
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
            ops=ops,
        )

    except asyncio.CancelledError:
        log.info("auto_rebase.cancelled", issue=issue_number, pr=pr_number)
        if task_run is not None:
            await ops.finalize_run(task_run.id, "failed", "Rebase cancelled", head_sha, session_factory)
        raise

    except Exception as exc:  # noqa: BLE001 (rebase spans git, LLM and DB; any failure is recorded on the run)
        log.exception("auto_rebase.unexpected_error", issue=issue_number, pr=pr_number)
        if task_run is not None:
            await ops.finalize_run(task_run.id, "failed", str(exc), head_sha, session_factory)
        return {"status": "failed", "pr_number": pr_number, "error": str(exc)}

    finally:
        if worktree_path is not None and not reused_worktree:
            try:
                await ops.cleanup_worktree(worktree_path, cwd=project_dir)
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
    ops: RebaseOperations,
) -> tuple[Path, bool] | dict[str, Any]:
    """Find or create the worktree to rebase *branch* in.

    Returns ``(worktree_path, reused)`` when the caller should continue, or an
    already-finalized result dict when the caller should return early (the
    worktree is busy, or the safety check itself failed).
    """
    prune_result = await ops.run("git", "worktree", "prune", cwd=project_dir)
    if not prune_result.success:
        log.warning("auto_rebase.prune_failed", stderr=prune_result.stderr[:200])

    existing_wt: Path | None = None
    try:
        existing_wt = await ops.find_worktree_by_branch(branch, cwd=project_dir)
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
        await ops.finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    if existing_wt is None:
        worktree_id = f"rebase-pr-{pr_number}"
        worktree_info = await ops.create_worktree(
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
        active_pid = await ops.check_worktree_active_agent(existing_wt, project_dir=project_dir)
    except RuntimeError as exc:
        log.warning(
            "auto_rebase.active_agent_check_failed",
            issue=issue_number,
            pr=pr_number,
            error=str(exc),
            exc_info=True,
        )
        reason = f"Cannot verify worktree safety: {exc}"
        await ops.finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "failed", "pr_number": pr_number, "error": str(exc)}

    if active_pid is not None:
        log.info("auto_rebase.worktree_busy", issue=issue_number, pr=pr_number, pid=active_pid)
        reason = f"Branch {branch} worktree is actively in use by agent PID {active_pid}"
        await ops.finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    if not await _worktree_is_clean(existing_wt, ops):
        # rebase_with_conflict_resolution() stashes and pops uncommitted
        # changes around the rebase; reusing a dirty worktree risks stranding
        # the developer's in-progress edits in the repo-wide stash if the
        # pop ever fails. Fail closed rather than risk that.
        log.warning("auto_rebase.worktree_dirty_or_unknown", issue=issue_number, pr=pr_number, path=str(existing_wt))
        reason = f"Branch {branch} worktree has uncommitted changes or its status could not be verified"
        await ops.finalize_run(task_run.id, "failed", reason, head_sha, session_factory, attempted=False)
        return {"status": "skipped", "pr_number": pr_number, "reason": reason}

    log.info("auto_rebase.reusing_worktree", issue=issue_number, pr=pr_number, path=str(existing_wt))
    return existing_wt, True


async def _worktree_is_clean(worktree_path: Path, ops: RebaseOperations) -> bool:
    """Check whether *worktree_path* has no uncommitted changes.

    Returns False when the status can't be read, so an unreadable worktree
    is never reused: `rebase_with_conflict_resolution()` stashes changes on a
    dirty tree, and reuse must fail closed rather than gamble on a clean one.
    """
    status_result = await ops.run("git", "status", "--porcelain", cwd=worktree_path)
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
    ops: RebaseOperations,
) -> dict[str, Any]:
    """Run the rebase, validate via pre-push hook, and push. Always returns a result dict."""
    result, cost = await ops.rebase_with_conflict_resolution(
        base_branch, cwd=worktree_path, project_dir=project_dir, max_commits=REBASE_MAX_COMMITS
    )
    await ops.update_run_cost(task_run.id, cost, session_factory)

    if not result.success:
        log.warning("auto_rebase.failed", issue=issue_number, pr=pr_number, error=result.error)
        await ops.finalize_run(task_run.id, "failed", result.error, head_sha, session_factory)
        ops.write_manual_handoff(project_dir, issue_number, pr_number, branch, f"Auto-rebase failed: {result.error}")
        return {"status": "failed", "pr_number": pr_number, "error": result.error}

    hook_result = await ops.run_pre_push_hook(worktree_path, project_dir=project_dir)
    if not hook_result["passed"]:
        log.warning("auto_rebase.pre_push_failed", issue=issue_number, pr=pr_number, output=hook_result["output"][:500])
        await ops.finalize_run(task_run.id, "failed", "Pre-push hook failed", head_sha, session_factory)
        ops.write_manual_handoff(
            project_dir,
            issue_number,
            pr_number,
            branch,
            "Rebase succeeded but pre-push hook failed",
            validation_error=hook_result["output"],
        )
        return {"status": "failed", "pr_number": pr_number, "error": "Pre-push hook failed after rebase"}

    push_result = await ops.run(
        "git",
        "push",
        "origin",
        branch,
        "--force-with-lease",
        cwd=worktree_path,
        env=scrub_check_env(project_dir=project_dir, quiet=True),
    )
    if not push_result.success:
        if "stale info" in push_result.stderr or "rejected" in push_result.stderr:
            log.info("auto_rebase.branch_changed", issue=issue_number, pr=pr_number)
            await ops.finalize_run(task_run.id, "done", "Branch HEAD changed during rebase", head_sha, session_factory)
            return {"status": "skipped", "pr_number": pr_number, "reason": "Branch HEAD changed during rebase"}
        await ops.finalize_run(task_run.id, "failed", push_result.stderr[:500], head_sha, session_factory)
        return {"status": "failed", "pr_number": pr_number, "error": push_result.stderr[:500]}

    log.info("auto_rebase.success", issue=issue_number, pr=pr_number, conflicts_resolved=result.conflicts_resolved)
    await ops.finalize_run(task_run.id, "done", None, head_sha, session_factory)
    return {"status": "success", "pr_number": pr_number, "conflicts_resolved": result.conflicts_resolved}


async def _get_pr_info(issue_number: int, repo: str, github_user: str) -> dict[str, Any] | None:
    """Fetch PR number, branch, and HEAD SHA for an issue.

    Returns None only for a confirmed empty search. Raises PRLookupError when the search or
    the `gh pr view` call fails or returns an unusable result, so a failed lookup is never
    reported as "no PR found".
    """
    pr = await find_pr_for_issue_checked(str(issue_number), repo=repo, github_user=github_user)
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
        env=await resolve_gh_env(github_user),
    )
    if not head_result.success:
        raise PRLookupError(f"gh pr view failed for PR #{pr.number} in {repo!r}: {head_result.stderr[:200]}")

    try:
        data = json.loads(head_result.stdout)
    except ValueError:
        raise PRLookupError(f"gh pr view returned unparseable output for PR #{pr.number} in {repo!r}") from None

    if not isinstance(data, dict):
        data = {}
    branch = data.get("headRefName")
    head_sha = data.get("headRefOid")
    if not branch or not head_sha:
        raise PRLookupError(f"gh pr view returned no head ref for PR #{pr.number} in {repo!r}")

    return {
        "number": pr.number,
        "branch": branch,
        "head_sha": head_sha,
        "base_branch": data.get("baseRefName", ""),
    }


async def _already_attempted(
    pr_number: int, head_sha: str, session_factory: async_sessionmaker, *, retry_failed: bool = False
) -> bool:
    """Check if we already attempted a rebase for this PR at this HEAD SHA.

    Ignores safety skips (busy worktree, dirty-or-unreadable worktree, a
    failed safety check, a rival run already in progress, or the branch
    sitting in the main checkout): those are transient conditions that can
    clear before the next poll, and counting them as an attempt would strand
    the PR until a new commit changes head_sha, since none of them ever ran
    the actual rebase.
    A dashboard click may also retry a failed run, while automated callers
    retain the failed attempt as a guard against repeated retries. A run still
    ``running`` has no ``head_sha`` yet and is not an attempt here;
    `_find_running_rival()` handles it.
    """
    async with session_factory() as session:
        stmt = select(TaskRun).where(TaskRun.role == ROLE_SUPERVISOR_REBASE, TaskRun.pr_number == pr_number)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        for r in runs:
            if not r.handoff_json or r.handoff_json.get("head_sha") != head_sha:
                continue
            if r.handoff_json.get("attempted", True):
                if retry_failed and r.status == TaskStatus.FAILED:
                    continue
                return True
    return False


async def _find_running_rival(
    run_id: int, pr_number: int, session_factory: async_sessionmaker, *, stale_after: timedelta
) -> TaskRun | None:
    """Return the earliest fresh `running` rebase row of this PR that started before *run_id*, if any.

    Called after *run_id* is committed, so two overlapping claims always see each other and only the
    lowest id proceeds. Rows older than *stale_after* are orphans of a crash and ignored.
    """
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        stmt = (
            select(TaskRun)
            .where(
                TaskRun.role == ROLE_SUPERVISOR_REBASE,
                TaskRun.pr_number == pr_number,
                TaskRun.status == TaskStatus.RUNNING,
                TaskRun.id < run_id,
            )
            .order_by(TaskRun.id)
        )
        for r in (await session.execute(stmt)).scalars().all():
            if _is_fresh_running(r.started_at, now, stale_after):
                return r
    return None


def _is_fresh_running(started_at: datetime | None, now: datetime, stale_after: timedelta) -> bool:
    """True when a running row started within *stale_after*, so it is not an orphan.

    A missing start time cannot be aged, so it counts as in progress.
    """
    if started_at is None:
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return now - started_at < stale_after


def _started_at_suffix(started_at: datetime | None) -> str:
    """Render ` (started HH:MM UTC)` for a skip reason, or nothing when the start time is unknown."""
    if started_at is None:
        return ""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return f" (started {started_at.astimezone(timezone.utc):%H:%M} UTC)"


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
            status=TaskStatus.RUNNING,
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


async def _run_pre_push_hook(worktree_path: Path, project_dir: Path | None = None) -> dict[str, Any]:
    """Run the pre-push hook in the worktree. Returns {passed, output}.

    *project_dir* picks the config the pytest worker cap comes from; the server's cwd may be another project.
    """
    hook_path = worktree_path / ".githooks" / "pre-push"
    if not hook_path.exists():
        return {"passed": True, "output": ""}

    result = await run(
        str(hook_path), cwd=worktree_path, timeout=PRE_PUSH_HOOK_TIMEOUT, env=scrub_check_env(project_dir=project_dir)
    )
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


def _default_ops() -> RebaseOperations:
    """Return the real collaborators attempt_auto_rebase() uses when a caller passes no `ops`.

    Built per call rather than once at import, so a test that patches one of these
    module-level names (`patch("sova.supervisor.rebase.cleanup_worktree")`) still
    reaches attempt_auto_rebase() instead of being bypassed by a captured reference.
    """
    return RebaseOperations(
        load_config=load_config,
        get_pr_info=_get_pr_info,
        already_attempted=_already_attempted,
        create_worktree=create_worktree,
        find_worktree_by_branch=find_worktree_by_branch,
        check_worktree_active_agent=check_worktree_active_agent,
        cleanup_worktree=cleanup_worktree,
        rebase_with_conflict_resolution=rebase_with_conflict_resolution,
        create_rebase_run=_create_rebase_run,
        find_running_rival=_find_running_rival,
        update_run_cost=_update_run_cost,
        finalize_run=_finalize_run,
        run_pre_push_hook=_run_pre_push_hook,
        run=run,
        write_manual_handoff=_write_manual_handoff,
    )

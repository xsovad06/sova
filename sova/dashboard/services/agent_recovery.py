"""Stale run detection, PID liveness checks, and interrupted run management."""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from cachetools import TTLCache
from sqlalchemy.exc import SQLAlchemyError

from sova.utils.logging import get_logger
from sova.utils.process import is_process_alive

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(component="dashboard.control.recovery")

_SENTINEL_NO_PR = -1  # cached "no PR exists" marker (distinct from None = not cached)

_SYNTHESIS_TTL_SECONDS = 60
# TTL cache for synthesized PR actions: (issue, pr) -> actions list or None
_synthesis_cache: TTLCache[tuple[str, int], list[dict] | None] = TTLCache(maxsize=256, ttl=_SYNTHESIS_TTL_SECONDS)
# Issue-level cache to avoid find_pr_for_issue shell call on cache hit
_issue_pr_cache: TTLCache[str, int | None] = TTLCache(maxsize=256, ttl=_SYNTHESIS_TTL_SECONDS)
# Reentrant lock protecting both caches from concurrent thread access
_cache_lock = threading.RLock()


def _check_issue_cache(issue_number: str) -> tuple[bool, int | None, list[dict] | None]:
    """Check issue-level and synthesis caches.

    Returns (fully_resolved, pr_number_or_None, result).
    - fully_resolved=True, pr=None, result=None: no PR exists (sentinel cached)
    - fully_resolved=True, pr=N, result=actions: synthesis cache hit
    - fully_resolved=False, pr=N, result=None: PR known but synthesis not cached
    - fully_resolved=False, pr=None, result=None: issue cache miss entirely
    """
    with _cache_lock:
        try:
            cached_pr = _issue_pr_cache[issue_number]
        except KeyError:
            return False, None, None
        if cached_pr == _SENTINEL_NO_PR:
            return True, None, None
        if cached_pr is not None:
            try:
                synth_result = _synthesis_cache[(issue_number, cached_pr)]
                return True, cached_pr, synth_result
            except KeyError:
                pass
            # PR number is known from cache; synthesis cache miss means we skip find_pr shell call
            return False, cached_pr, None
        return False, None, None


def _deduplicate_reviews(reviews: list) -> dict:
    """Keep latest review per reviewer, using timestamp comparison."""
    from sova.adapters.base import PRReview  # noqa: F811

    latest_by_reviewer: dict[str, PRReview] = {}
    for review in reviews:
        existing = latest_by_reviewer.get(review.reviewer)
        if existing is None:
            latest_by_reviewer[review.reviewer] = review
            continue
        try:
            new_ts = datetime.fromisoformat(review.submitted_at.replace("Z", "+00:00"))
            old_ts = datetime.fromisoformat(existing.submitted_at.replace("Z", "+00:00"))
            if new_ts > old_ts:
                latest_by_reviewer[review.reviewer] = review
        except (ValueError, AttributeError):
            if review.submitted_at > existing.submitted_at:
                latest_by_reviewer[review.reviewer] = review
    return latest_by_reviewer


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.

    Thin wrapper for backward compatibility. New code should import from sova.utils.process.
    """
    return is_process_alive(pid)


async def _kill_process(pid: int) -> None:
    """Send SIGTERM, wait briefly, then SIGKILL if still alive."""
    import asyncio
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    await asyncio.sleep(2)
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


_MERGE_CHECK_TIMEOUT = 10.0  # per-PR GitHub check during recovery
_RECOVERY_TOTAL_TIMEOUT = 20.0  # hard cap on the entire recover_stale_runs() call


_ZOMBIE_RECENCY_HOURS = 24


def _is_zombie_process(pid: int, started_at: datetime | None) -> bool:
    """Verify a PID belongs to the expected agent process, not a recycled PID.

    Compares the process creation time against the TaskRun's started_at to guard
    against killing unrelated processes that inherited a recycled PID.
    """
    try:
        import psutil

        proc = psutil.Process(pid)
        create_time = datetime.fromtimestamp(proc.create_time(), tz=timezone.utc)
        if started_at is not None:
            started_utc = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
            if create_time > started_utc + timedelta(minutes=5):
                log.info(
                    "recovery.pid_recycled",
                    pid=pid,
                    proc_created=create_time.isoformat(),
                    run_started=started_utc.isoformat(),
                )
                return False
        return True
    except Exception:  # noqa: BLE001 (psutil import or process lookup can fail many ways; fall back to a plain PID check)
        log.debug("recovery.pid_probe_failed", pid=pid, exc_info=True)
        return _is_process_alive(pid)


def _get_managed_run_ids() -> tuple[set[int], bool]:
    """Return (managed_run_ids, loaded_ok) from the in-memory agent pool.

    Returns an empty set with loaded_ok=False if the pool is unavailable,
    so callers can decide how to handle unknown managed state.
    """
    try:
        from sova.dashboard.services.agent_pool import _projects

        ids: set[int] = set()
        for pa in _projects.values():
            ids.update(pa.agents.keys())
        return ids, True
    except Exception:  # noqa: BLE001 (pool snapshot is best-effort; callers fall back to DB-only recovery)
        log.warning("recovery.managed_run_ids_failed", exc_info=True)
        return set(), False


async def _kill_terminal_zombies(project_dir: Path | None = None) -> int:
    """Kill processes attached to terminal TaskRuns that are still alive.

    When the inner subprocess (sova run) finalizes a TaskRun to terminal status
    but the outer claude -p process never exits, and the dashboard restarts,
    the process becomes invisible to all recovery mechanisms (they only query
    non-terminal runs). This function scans recent terminal runs with PIDs and
    kills any that are still alive and match the expected process identity.

    Returns the number of zombie processes killed.
    """
    import asyncio

    from sqlalchemy import func, select

    from sova.dashboard.services.work_service import _TERMINAL
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    managed_run_ids, _ = _get_managed_run_ids()

    killed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_ZOMBIE_RECENCY_HOURS)
        async with await get_session(project_dir=project_dir) as session:
            stmt = select(TaskRun.id, TaskRun.pid, TaskRun.started_at).where(
                TaskRun.status.in_(_TERMINAL),
                TaskRun.pid.isnot(None),
                func.coalesce(TaskRun.ended_at, TaskRun.started_at) >= cutoff,
            )
            result = await session.execute(stmt)
            terminal_with_pid = result.fetchall()

        kill_tasks = []
        for run_id, pid, started_at in terminal_with_pid:
            if not pid or run_id in managed_run_ids:
                continue
            if _is_zombie_process(pid, started_at):
                log.warning("recovery.killing_terminal_zombie", run_id=run_id, pid=pid)
                kill_tasks.append(_kill_process(pid))
        if kill_tasks:
            await asyncio.gather(*kill_tasks, return_exceptions=True)
            killed = len(kill_tasks)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("recovery.terminal_zombie_scan_failed", exc_info=True)

    if killed:
        log.info("recovery.terminal_zombies_killed", count=killed)
    return killed


def _get_recovery_config(project_dir: Path | None) -> tuple[str, str]:
    """Load (repo, github_user) for merge queue checks during recovery."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.github_repo or "", cfg.github_user or ""
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("recovery.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return "", ""


async def _get_pr_branch_for_recovery(pr_number: int, repo: str, project_dir: Path | None) -> str:
    """Fetch the head branch name for a PR (for worktree cleanup later)."""
    if not repo:
        return ""
    try:
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import run

        _, github_user = _get_recovery_config(project_dir)
        env = await resolve_gh_env(github_user)
        result = await run(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
            env=env,
        )
        if result.success and result.stdout.strip():
            return result.stdout.strip()
    except (RuntimeError, OSError):
        log.debug("recovery.pr_branch_lookup_failed", pr=pr_number, exc_info=True)
    return ""


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale non-terminal TaskRuns on dashboard startup.

    Three-phase approach to keep startup fast regardless of stale-run count:
      1. Read-only query + handoff checks (synchronous, fast).
      2. Merge checks for crashed merge-role runs -- run concurrently, total
         budget capped at ``_RECOVERY_TOTAL_TIMEOUT`` seconds. Previously these
         ran sequentially inside a write transaction, causing N×15s freezes.
      3. Single write transaction to persist all status updates.
    """
    import asyncio

    try:
        from decimal import Decimal

        from sqlalchemy import select

        from sova.dashboard.services.agent_lifecycle import (
            _MERGE_ROLES,
            _check_pr_merged_on_failure,
        )
        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        # Phase 1: load stale runs and apply fast (synchronous) checks.
        async with await get_session(project_dir=project_dir) as session:
            stmt = select(TaskRun).where(TaskRun.status.notin_(_TERMINAL))
            result = await session.execute(stmt)
            all_stale = result.scalars().all()

        # Collect dead runs with their snapshot data for writing back later.
        # We deliberately work outside a session here so the write lock is free.
        # Build set of run_ids managed by the current dashboard instance so we
        # can distinguish "alive and managed" from "alive but orphaned".
        managed_run_ids, _managed_loaded = _get_managed_run_ids()

        orphan_kill_tasks: list[asyncio.Task[None]] = []
        dead_runs: list[dict] = []
        for run in all_stale:
            if run.pid and _is_zombie_process(run.pid, run.started_at):
                if run.id in managed_run_ids:
                    log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                    continue
                if not _managed_loaded:
                    log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                    continue
                log.warning("recovery.killing_orphan", run_id=run.id, pid=run.pid)
                orphan_kill_tasks.append(asyncio.ensure_future(_kill_process(run.pid)))

            was_status = run.status
            final_status = "interrupted"
            cost_override = None
            error_msg = None

            # Handoff check: if agent wrote a handoff after the run started, mark done.
            # Skip issue-less runs: get_handoff(issue=None) returns the project-wide
            # latest handoff, which belongs to a different run.
            try:
                from sova.dashboard.services import handoff_service

                hf = handoff_service.get_handoff(project_dir, issue=run.issue_number) if run.issue_number else None
                if hf and hf.get("status") == "awaiting_action":
                    hf_time_str = hf.get("created_at")
                    run_start = run.started_at or datetime.min.replace(tzinfo=timezone.utc)
                    if run_start.tzinfo is None:
                        run_start = run_start.replace(tzinfo=timezone.utc)
                    hf_dt = None
                    if hf_time_str:
                        hf_dt = datetime.fromisoformat(hf_time_str.replace("Z", "+00:00"))
                        if hf_dt.tzinfo is None:
                            hf_dt = hf_dt.replace(tzinfo=timezone.utc)
                    if hf_dt is not None and hf_dt >= run_start:
                        final_status = "done"
                        cost = hf.get("details", {}).get("cost_usd")
                        if cost is not None:
                            cost_override = Decimal(str(cost))
                        log.info("recovery.completed_with_handoff", run_id=run.id, issue=run.issue_number)
            except Exception:  # noqa: BLE001 (fail-open: handoff check spans file I/O, JSON parsing and date math)
                log.debug("recovery.handoff_check_failed", run_id=run.id, exc_info=True)

            _role_parts = (run.role or "").removeprefix("command:").removeprefix("/").split()
            needs_merge_check = (
                final_status == "interrupted"
                and run.pr_number is not None
                and bool(_role_parts)
                and _role_parts[0] in _MERGE_ROLES
            )

            dead_runs.append(
                {
                    "run_id": run.id,
                    "pr_number": run.pr_number,
                    "issue": run.issue_number,
                    "role": run.role,
                    "pid": run.pid,
                    "was_status": was_status,
                    "final_status": final_status,
                    "cost_override": cost_override,
                    "error_msg": error_msg,
                    "needs_merge_check": needs_merge_check,
                }
            )

        if orphan_kill_tasks:
            await asyncio.gather(*orphan_kill_tasks, return_exceptions=True)

        if not dead_runs:
            return []

        # Phase 2: merge checks concurrently, bounded by the total timeout.
        # Also checks merge queue status for non-merged PRs so we can create
        # MergeQueueEntry records instead of marking runs "interrupted".
        merge_candidates = [r for r in dead_runs if r["needs_merge_check"]]
        if merge_candidates:

            async def _check_one(rec: dict) -> tuple[int, str, dict | None]:
                """Returns (run_id, result_type, extra_data).

                result_type: "merged", "in_queue", or "unknown".
                """
                try:
                    merged = await asyncio.wait_for(
                        _check_pr_merged_on_failure(rec["pr_number"], project_dir),
                        timeout=_MERGE_CHECK_TIMEOUT,
                    )
                    if merged:
                        return rec["run_id"], "merged", None
                except (RuntimeError, OSError):
                    log.debug("recovery.merge_check_skipped", run_id=rec["run_id"], exc_info=True)
                    return rec["run_id"], "unknown", None

                try:
                    from sova.git.merge import get_merge_queue_status

                    _repo, _user = _get_recovery_config(project_dir)
                    queue_status = await asyncio.wait_for(
                        get_merge_queue_status(
                            rec["pr_number"],
                            repo=_repo,
                            github_user=_user,
                        ),
                        timeout=_MERGE_CHECK_TIMEOUT,
                    )
                    if queue_status.in_queue:
                        return rec["run_id"], "in_queue", {"state": queue_status.state}
                    if queue_status.is_merged:
                        return rec["run_id"], "merged", None
                except (RuntimeError, OSError):
                    log.debug("recovery.queue_check_skipped", run_id=rec["run_id"], exc_info=True)

                return rec["run_id"], "unknown", None

            dead_by_id = {r["run_id"]: r for r in dead_runs}
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*(_check_one(r) for r in merge_candidates)),
                    timeout=_RECOVERY_TOTAL_TIMEOUT,
                )
                for run_id, result_type, _extra in results:
                    rec = dead_by_id[run_id]
                    if result_type == "merged":
                        rec["final_status"] = "done"
                        rec["error_msg"] = f"Agent process died but PR #{rec['pr_number']} was merged successfully"
                        log.info("recovery.merge_succeeded_despite_crash", run_id=run_id, pr=rec["pr_number"])
                    elif result_type == "in_queue":
                        rec["final_status"] = "done"
                        rec["error_msg"] = f"PR #{rec['pr_number']} is in merge queue, tracked for post-merge cleanup"
                        rec["needs_queue_entry"] = True
                        log.info("recovery.pr_in_merge_queue", run_id=run_id, pr=rec["pr_number"])
            except asyncio.TimeoutError:
                log.warning("recovery.merge_checks_timed_out", total_timeout=_RECOVERY_TOTAL_TIMEOUT)

        # Phase 3: single fast write transaction.
        interrupted = []
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.id.in_([r["run_id"] for r in dead_runs]))
                result = await session.execute(stmt)
                runs_by_id = {r.id: r for r in result.scalars().all()}

                for rec in dead_runs:
                    run = runs_by_id.get(rec["run_id"])
                    if run is None:
                        continue
                    if run.status in _TERMINAL:
                        continue  # finalized by a concurrent writer between Phase 1 and Phase 3
                    final_status = rec["final_status"]
                    run.status = final_status
                    run.ended_at = datetime.now(timezone.utc)
                    if final_status == "interrupted":
                        run.error_message = f"Stale run recovered on startup (was {rec['was_status']!r})"
                        interrupted.append(
                            {
                                "run_id": run.id,
                                "issue": run.issue_number,
                                "role": run.role,
                                "pid": run.pid,
                            }
                        )
                    else:
                        run.error_message = rec["error_msg"]
                        if rec["cost_override"] is not None:
                            run.total_cost_usd = rec["cost_override"]
                    log.warning(
                        "recovery.stale_run",
                        run_id=run.id,
                        issue=run.issue_number,
                        pid=run.pid,
                        was_status=rec["was_status"],
                        final_status=final_status,
                    )

        # Phase 4: create MergeQueueEntry records for PRs still in the queue.
        # Done outside the write transaction to avoid blocking on API calls.
        queue_candidates = [r for r in dead_runs if r.get("needs_queue_entry")]
        if queue_candidates:
            from sova.dashboard.services.merge_queue_monitor import create_merge_queue_entry

            repo, github_user = _get_recovery_config(project_dir)
            for rec in queue_candidates:
                try:
                    branch = await _get_pr_branch_for_recovery(rec["pr_number"], repo, project_dir)
                    await create_merge_queue_entry(
                        pr_number=rec["pr_number"],
                        repo=repo,
                        project_dir=project_dir or Path.cwd(),
                        issue_number=rec.get("issue"),
                        task_run_id=rec["run_id"],
                        github_user=github_user,
                        branch_name=branch,
                    )
                except (RuntimeError, OSError, SQLAlchemyError):
                    log.warning("recovery.queue_entry_failed", run_id=rec["run_id"], exc_info=True)

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))

        # Rollback issue states for interrupted runs (outside DB transaction)
        for rec in interrupted:
            try:
                await rollback_issue_state(rec["run_id"], project_dir)
            except Exception:  # noqa: BLE001 (rollback is best-effort cleanup; recovery continues either way)
                log.debug("recovery.rollback_failed", run_id=rec["run_id"], exc_info=True)

        return interrupted
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("recovery.failed", exc_info=True)
        return []


async def rollback_issue_state(run_id: int, project_dir: Path | None = None) -> None:
    """Roll back issue state on agent failure.

    Maps the failed agent's role to the appropriate prior state and calls
    adapter.transition_state(). Skips rollback when:
    - issue_number is missing (logs and returns)
    - Another non-terminal run exists for the same issue (concurrent work)
    - GitHub API fails (logs warning, non-fatal)

    Special cases:
    - Triage role: removes all agent: labels instead of transitioning
    - Command roles: extracts base role from command:X format
    - Developer with PR: rolls back to in_review (not in_progress)
    """
    try:
        from sqlalchemy import select

        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config
        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return

            issue_number = task_run.issue_number
            if not issue_number:
                log.debug("rollback.no_issue", run_id=run_id)
                return

            role = task_run.role or ""
            pr_number = task_run.pr_number

            # Check for concurrent runs on the same issue
            stmt = (
                select(TaskRun.id)
                .where(
                    TaskRun.issue_number == issue_number,
                    TaskRun.id != run_id,
                    TaskRun.status.notin_(_TERMINAL),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            if result.scalar_one_or_none() is not None:
                log.info("rollback.skipped_concurrent_run", run_id=run_id, issue=issue_number)
                return

        # Load adapter outside DB session (GitHub API calls)
        config = load_config(project_dir)
        adapter = create_adapter(config)

        # Parse role for command variants
        role_clean = role.removeprefix("command:").removeprefix("/").split()[0]

        # Special case: triage role removes all agent: labels
        if role_clean == "triage":
            try:
                task = await adapter.get_task(issue_number)
                agent_labels = [label for label in task.labels if label.startswith("agent:")]
                for label in agent_labels:
                    await adapter.remove_label(issue_number, label)
                log.info("rollback.triage_labels_removed", issue=issue_number, count=len(agent_labels))
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("rollback.triage_failed", issue=issue_number, exc_info=True)
            return

        # Map role to rollback state
        rollback_state_map = {
            "researcher": TaskState.TRIAGED,
            "reviewer": TaskState.IN_REVIEW,
            "integrate-pr": TaskState.IN_REVIEW,
            "approve-merge": TaskState.IN_REVIEW,
            "address-pr": TaskState.IN_REVIEW,
        }

        # Developer maps to different states based on PR existence
        if role_clean == "developer":
            target_state = TaskState.IN_REVIEW if pr_number else TaskState.RESEARCHED
        else:
            target_state = rollback_state_map.get(role_clean)

        if target_state is None:
            log.debug("rollback.unknown_role", role=role, issue=issue_number)
            return

        await adapter.transition_state(issue_number, target_state)
        log.info("rollback.completed", run_id=run_id, issue=issue_number, target_state=target_state.value)
    except Exception:  # noqa: BLE001 (config load, DB read and tracker call each fail differently)
        log.warning("rollback.failed", run_id=run_id, exc_info=True)


async def get_interrupted_runs(limit: int = 5) -> list[dict]:
    """Get recently interrupted task runs."""
    from sova.dashboard.services import run_service
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                return await run_service.list_runs(session, status="interrupted", limit=limit)
    except (OSError, RuntimeError, SQLAlchemyError):
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
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("dismiss_interrupted.failed", exc_info=True)
        return 0


_VERDICT_INLINE = [
    (re.compile(r"\*?\*?Verdict\*?\*?\s*:?\s*\*?\*?Approve\b", re.IGNORECASE), "approve"),
    (re.compile(r"\*?\*?Verdict\*?\*?\s*:?\s*\*?\*?Request\s+changes\b", re.IGNORECASE), "revise"),
    (re.compile(r"\*?\*?Verdict\*?\*?\s*:?\s*\*?\*?Comment\s+only\b", re.IGNORECASE), "revise"),
]
_VERDICT_VALUE = [
    (re.compile(r"^\s*[-*]*\s*\*?\*?Approve\b", re.IGNORECASE), "approve"),
    (re.compile(r"^\s*[-*]*\s*\*?\*?Request\s+changes\b", re.IGNORECASE), "revise"),
    (re.compile(r"^\s*[-*]*\s*\*?\*?Comment\s+only\b", re.IGNORECASE), "revise"),
]
_VERDICT_HEADING = re.compile(r"#{1,4}\s*\*?\*?Verdict\*?\*?\s*$", re.IGNORECASE)


def _parse_verdict_from_output(lines: list[str]) -> str | None:
    """Extract the review verdict from agent output text.

    Handles two formats:
    - Single-line: "Verdict: Approve", "**Verdict: Request changes**"
    - Multi-line: "### Verdict" heading followed by "**Approve**" on a later line

    Returns "approve", "revise", or None if no verdict pattern is found.
    """
    for line in reversed(lines):
        for pattern, result in _VERDICT_INLINE:
            if pattern.search(line):
                return result

    for i in range(len(lines) - 1, -1, -1):
        if _VERDICT_HEADING.search(lines[i]):
            for j in range(i + 1, min(i + 4, len(lines))):
                for pattern, result in _VERDICT_VALUE:
                    if pattern.search(lines[j]):
                        return result
            break

    return None


def _clean_issue_scope(issue_number: str | None, pr_number: int | None) -> tuple[bool, str | None]:
    """Normalize the caller's (issue_number, pr_number) into (has_scope, issue_num_clean).

    Strips a leading ``#`` and whitespace from the issue. ``has_scope`` is
    False when neither key is usable (no issue and no PR, or a blank issue
    with no PR), in which case the caller has nothing to query for.
    """
    if issue_number is None and pr_number is None:
        return False, None
    issue_num_clean = issue_number.lstrip("#").strip() if issue_number is not None else None
    if issue_num_clean == "":
        if pr_number is None:
            return False, None
        issue_num_clean = None  # fall through to PR-only scope
    return True, issue_num_clean


def _run_scope_filters(issue_num_clean: str | None, pr_number: int | None) -> list:
    """SQL filters selecting the TaskRuns that belong to one review cycle.

    A PR number identifies the cycle exactly, so when one is known it is the
    only key. Requiring the issue to match as well only adds false negatives:
    a run spawned before the PR body linked its issue, or from a standalone-PR
    work item, is recorded with ``issue_number=NULL`` yet is unambiguously a
    run against this PR (#1063 was addressed by exactly such a run, and the
    issue-and-PR filter could not see it). Without a PR number the issue is
    the only key available.
    """
    from sova.db.models import TaskRun

    if pr_number is not None:
        return [TaskRun.pr_number == pr_number]
    return [TaskRun.issue_number == issue_num_clean]


async def _address_cycle_completed_since(
    session: "AsyncSession", since: datetime, issue_num_clean: str | None, pr_number: int | None
) -> bool:
    """True when a completed address cycle for this PR/issue finished at or after ``since``.

    Two shapes count as a completed address cycle: a ``command:address-pr``
    run, or a ``developer`` run whose pipeline included a completed
    ``address_review`` step (the autonomous address-review pipeline).
    """
    from sqlalchemy import and_, exists, func, or_, select

    from sova.db.models import StepExecution, TaskRun

    scope = _run_scope_filters(issue_num_clean, pr_number)
    finished_at = func.coalesce(TaskRun.ended_at, TaskRun.started_at)
    command_cycle = and_(
        TaskRun.role == "command:address-pr",
        TaskRun.status == "done",
        finished_at >= since,
        *scope,
    )
    pipeline_cycle = and_(
        TaskRun.role == "developer",
        TaskRun.status == "done",
        finished_at >= since,
        exists(
            select(1).where(
                StepExecution.task_run_id == TaskRun.id,
                StepExecution.step_name == "address_review",
                StepExecution.status == "done",
            )
        ),
        *scope,
    )
    result = await session.execute(select(func.count()).select_from(TaskRun).where(or_(command_cycle, pipeline_cycle)))
    return result.scalar_one() > 0


async def has_address_cycle_since(
    since: datetime,
    issue_number: str | None,
    *,
    pr_number: int | None,
    project_dir: "Path | None" = None,
) -> bool:
    """Whether this machine's DB records a completed address cycle at or after ``since``.

    Lets a verdict that did not come from the local DB (a ``sova-review``
    marker found on GitHub, e.g. one posted by the ``/review-pr`` command or by
    another SOVA instance) still be superseded by an address cycle this
    machine ran. Fails open to ``False``: an unreadable DB leaves the verdict
    standing rather than claiming it was addressed.
    """
    from sova.db.session import get_session

    has_scope, issue_num_clean = _clean_issue_scope(issue_number, pr_number)
    if not has_scope:
        return False
    try:
        async with await get_session(project_dir=project_dir) as session:
            return await _address_cycle_completed_since(session, since, issue_num_clean, pr_number)
    except Exception:  # noqa: BLE001 (DB failure must leave the verdict standing, never fake an address cycle)
        log.debug("has_address_cycle_since.failed", issue=issue_number, pr=pr_number, exc_info=True)
        return False


async def get_sova_review_verdict(
    issue_number: str | None, *, pr_number: int | None = None, project_dir: "Path | None" = None
) -> dict:
    """Query the DB for the most recent SOVA reviewer verdict on an issue or PR.

    Returns adapter-agnostic review state from SOVA's own TaskRun records,
    independent of any platform-specific review mechanism (GitHub reviews, etc.).

    When pr_number is provided it is the only scope key: runs are matched on
    the PR alone, whether or not their issue_number is set (see
    _run_scope_filters). This prevents a reviewer verdict from a previous PR
    version being treated as current when the PR has since been updated by an
    address-review cycle, and it still finds runs recorded before the PR body
    linked its issue.

    When pr_number is None, issue_number must be provided and is the scope key.

    When a completed address cycle exists for the same issue/PR with a
    timestamp newer than the selected reviewer run, returns "addressed"
    immediately, superseding any verdict from the review run. An address
    cycle is either a completed `command:address-pr` run, or a completed
    `developer` run whose pipeline included a completed `address_review`
    step (the address-review pipeline path).

    When handoff_json is present, the verdict is derived from it (authoritative).
    When a review run completed successfully but has no handoff_json (e.g.
    command:review-pr which posts to GitHub but doesn't write handoff, or
    role="reviewer" runs where WorkflowEngine never adopted the TaskRun due to
    a pipeline bypass leaving current_step="agent"), the verdict is parsed from
    the agent's output lines.  Falls back to "revise" if the output contains no
    recognizable verdict pattern.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    no_review: dict = {
        "has_sova_review": False,
        "verdict": None,
        "finding_count": 0,
        "reviewed_at": None,
        "run_status": None,
        "review_head_sha": None,
    }

    has_scope, issue_num_clean = _clean_issue_scope(issue_number, pr_number)
    if not has_scope:
        return no_review

    scope = _run_scope_filters(issue_num_clean, pr_number)

    try:
        async with await get_session(project_dir=project_dir) as session:
            # First: look for runs WITH handoff_json (authoritative source).
            filters = [
                TaskRun.role.in_(["reviewer", "command:review-pr"]),
                TaskRun.status.in_(["done", "failed", "interrupted", "stopped"]),
                TaskRun.handoff_json.isnot(None),
                *scope,
            ]
            stmt = (
                select(TaskRun)
                .where(*filters)
                .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run = result.scalar_one_or_none()

            # Fallback: look for reviewer or command:review-pr runs WITHOUT handoff_json.
            # command:review-pr posts to GitHub but doesn't write structured handoff data.
            # reviewer runs may also have null handoff_json when WorkflowEngine never
            # adopted the TaskRun (pipeline bypass: current_step sentinel not cleared).
            if not run:
                fallback_filters = [
                    TaskRun.role.in_(["reviewer", "command:review-pr"]),
                    TaskRun.status == "done",
                    TaskRun.handoff_json.is_(None),
                    *scope,
                ]
                stmt = (
                    select(TaskRun)
                    .where(*fallback_filters)
                    .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                run = result.scalar_one_or_none()

            if not run:
                return no_review

            # If an address cycle completed after this review, the review cycle is
            # done: return "addressed" so a fresh review drives the display rather
            # than the stale pre-fix verdict.
            run_ts = run.ended_at or run.started_at
            superseded = await _address_cycle_completed_since(session, run_ts, issue_num_clean, pr_number)

            if superseded:
                return {
                    "has_sova_review": True,
                    "verdict": "addressed",
                    "finding_count": 0,
                    "reviewed_at": run_ts.isoformat() if run_ts else None,
                    "run_status": "done",
                    "review_head_sha": None,
                }

            handoff = run.handoff_json
            if handoff is not None:
                next_action = handoff.get("next_action", "")
                findings = handoff.get("pending_findings", [])
                metadata = handoff.get("metadata", {}) or {}

                if next_action == "approve":
                    verdict = "approve"
                elif next_action == "needs_human_review":
                    # Protected-path-only review: code quality passed but
                    # protected paths touched. Treat as approved for routing
                    # purposes (no address-review needed).
                    verdict = "approve"
                elif next_action == "review_post_failed":
                    # Reviewer ran but could not post to GitHub. Return a distinct
                    # verdict so callers do not trigger address-review pipeline.
                    verdict = "post_failed"
                elif metadata.get("verdict"):
                    # Authoritative: persisted by ReviewerRole._write_handoff()
                    # from the blocking (>= review.revise_severity) findings.
                    verdict = metadata["verdict"]
                elif findings:
                    # Fallback for handoffs written before metadata.verdict existed.
                    max_sev = max((f.get("severity", 0) for f in findings), default=0)
                    verdict = "block" if max_sev >= 7 else "revise"
                else:
                    verdict = "approve"

                return {
                    "has_sova_review": True,
                    "verdict": verdict,
                    "finding_count": len(findings),
                    "finding_summary": metadata.get("finding_summary"),
                    "reviewed_at": ts.isoformat() if (ts := run.ended_at or run.started_at) else None,
                    "run_status": run.status,
                    "review_head_sha": metadata.get("review_head_sha"),
                }

            # No handoff_json -- parse verdict from the agent's output lines.
            from sova.db.models import OutputLine

            output_stmt = (
                select(OutputLine.text).where(OutputLine.task_run_id == run.id).order_by(OutputLine.line_number)
            )
            output_result = await session.execute(output_stmt)
            output_lines = [row[0] for row in output_result.fetchall()]

            parsed_verdict = _parse_verdict_from_output(output_lines)
            verdict = parsed_verdict or "revise"

            return {
                "has_sova_review": True,
                "verdict": verdict,
                "finding_count": 0,
                "reviewed_at": ts.isoformat() if (ts := run.ended_at or run.started_at) else None,
                "run_status": run.status,
                "review_head_sha": None,
            }
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("sova_review_verdict.query_failed", issue=issue_number, exc_info=True)
        return no_review


def _summarize_ci_checks(checks: list | None) -> str:
    """Summarize CI check results into a single status string."""
    from sova.git.operations import CheckConclusion, CheckStatus

    if checks is None:
        return "unknown"
    if not checks:
        return "none"
    if all(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.SUCCESS for c in checks):
        return "passed"
    if any(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.FAILURE for c in checks):
        return "failed"
    if any(c.status == CheckStatus.IN_PROGRESS for c in checks):
        return "pending"
    return "passed"


def _load_repo_config() -> tuple[str, str] | None:
    """Load project config and return (repo, gh_user), or None if unavailable."""
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    project_dir = get_project_dir()
    if not project_dir:
        return None
    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("recovery.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return None
    if not cfg.github_repo:
        return None
    return cfg.github_repo, cfg.github_user


async def get_pr_status_for_issue(issue_number: str) -> dict:
    """Get PR status for an issue -- approval state, CI, mergeability."""
    from sova.git.operations import find_pr_for_issue, get_ci_checks, get_pr_status

    repo_cfg = _load_repo_config()
    if not repo_cfg:
        return {"has_pr": False}
    repo, gh_user = repo_cfg

    pr_info = await find_pr_for_issue(issue_number, repo=repo, github_user=gh_user)
    if not pr_info:
        return {"has_pr": False}

    try:
        status = await get_pr_status(pr_info.number, repo=repo, github_user=gh_user)
    except (RuntimeError, OSError):
        log.debug("pr_status.fetch_failed", issue=issue_number, exc_info=True)
        return {"has_pr": True, "pr_number": pr_info.number, "error": "Failed to fetch PR status"}

    ci_summary = "unknown"
    try:
        checks = await get_ci_checks(pr_info.number, repo=repo, github_user=gh_user)
        ci_summary = _summarize_ci_checks(checks)
    except (RuntimeError, OSError):
        log.debug("ci_checks.fetch_failed", pr=pr_info.number, exc_info=True)

    from sova.dashboard.project_context import get_project_dir as _get_project_dir

    sova_review = await get_sova_review_verdict(issue_number, pr_number=status.number, project_dir=_get_project_dir())

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
        "sova_review": sova_review,
    }


async def _has_active_run(issue_number: str) -> bool:
    """Check if there are non-terminal TaskRuns for this issue."""
    from sqlalchemy import select

    from sova.dashboard.services.work_service import _TERMINAL
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            stmt = (
                select(TaskRun.id)
                .where(
                    TaskRun.issue_number == issue_number,
                    TaskRun.status.notin_(_TERMINAL),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None


def _interpret_reviews(latest_by_reviewer: dict) -> tuple[bool, int, int]:
    """Interpret review states, excluding dismissed and bot reviews.

    Returns (has_changes_requested, approvals, human_review_count).
    """
    has_changes_requested = False
    approvals = 0
    human_review_count = 0

    for review in latest_by_reviewer.values():
        if review.state == "DISMISSED" or review.is_bot:
            continue
        human_review_count += 1
        if review.state == "CHANGES_REQUESTED":
            has_changes_requested = True
        elif review.state == "APPROVED":
            approvals += 1

    return has_changes_requested, approvals, human_review_count


def _build_address_review_action(issue_number: str, pr_number: int) -> list[dict]:
    """Build the 'Address Review' action list."""
    return [
        {
            "id": "address_review",
            "label": "Address Review",
            "description": "Address review findings from PR reviewers",
            "style": "approve",
            "mode": "agent",
            "command": "",
            "args": {"issue": issue_number, "pr": pr_number, "role": "developer"},
            "auto_execute": False,
        },
    ]


def _build_integrate_actions(issue_number: str, pr_number: int) -> list[dict]:
    """Build the 'Integrate PR' action list."""
    return [
        {
            "id": "integrate",
            "label": "Integrate PR",
            "description": "All reviews approved -- rebase, merge, cleanup, and learn",
            "style": "approve",
            "mode": "claude-command",
            "command": f"/integrate-pr {pr_number}",
            "args": {"issue": issue_number, "pr": pr_number},
            "auto_execute": False,
        },
    ]


async def _fetch_and_interpret_reviews(issue_number: str, pr_number: int, cache_key: tuple) -> list[dict] | None:
    """Fetch reviews from adapter, deduplicate, interpret, and build actions."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    cfg = load_config(get_project_dir())
    try:
        adapter = create_adapter(cfg)
        reviews = await adapter.get_pr_reviews(pr_number)
    except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
        log.debug("synthesize.fetch_reviews_failed", issue=issue_number, exc_info=True)
        with _cache_lock:
            _synthesis_cache[cache_key] = None
        return None

    if not reviews:
        with _cache_lock:
            _synthesis_cache[cache_key] = None
        return None

    latest_by_reviewer = _deduplicate_reviews(reviews)
    has_changes_requested, approvals, human_review_count = _interpret_reviews(latest_by_reviewer)

    actions: list[dict] | None = None
    if has_changes_requested:
        actions = _build_address_review_action(issue_number, pr_number)
    elif approvals > 0 and approvals == human_review_count:
        actions = _build_integrate_actions(issue_number, pr_number)

    with _cache_lock:
        _synthesis_cache[cache_key] = actions
    return actions


async def synthesize_pr_actions(issue_number: str) -> list[dict] | None:
    """Synthesize HandoffAction-shaped dicts from PR review state.

    Returns None if no PR exists or no actionable review state found.
    Called only when no handoff file exists and no agent is running for the issue.
    """
    from sova.git.operations import find_pr_for_issue

    issue_number = issue_number.lstrip("#").strip()

    repo_cfg = _load_repo_config()
    if not repo_cfg:
        return None
    repo, gh_user = repo_cfg

    fully_resolved, cached_pr, cached_result = _check_issue_cache(issue_number)
    if fully_resolved:
        return cached_result

    # Use cached PR number if available, otherwise call find_pr_for_issue
    if cached_pr is not None:
        pr_number = cached_pr
    else:
        pr_info = await find_pr_for_issue(issue_number, repo=repo, github_user=gh_user)
        if not pr_info:
            with _cache_lock:
                _issue_pr_cache[issue_number] = _SENTINEL_NO_PR
            return None
        pr_number = pr_info.number
        with _cache_lock:
            _issue_pr_cache[issue_number] = pr_number

    cache_key = (issue_number, pr_number)
    with _cache_lock:
        try:
            return _synthesis_cache[cache_key]
        except KeyError:
            pass

    try:
        if await _has_active_run(issue_number):
            with _cache_lock:
                _synthesis_cache[cache_key] = None
            return None
    except Exception:  # noqa: BLE001 (fail-open: active-run check must not block PR action synthesis)
        log.debug("synthesize.active_run_check_failed", issue=issue_number, exc_info=True)

    return await _fetch_and_interpret_reviews(issue_number, pr_number, cache_key)


def invalidate_synthesis_cache(issue: str, pr: int) -> None:
    """Invalidate synthesized PR actions cache for an issue/PR pair."""
    with _cache_lock:
        _synthesis_cache.pop((issue, pr), None)
        _issue_pr_cache.pop(issue, None)


async def get_synthesized_handoff() -> dict | None:
    """Build a handoff-shaped dict from PR review state for recent done runs.

    Queries recent completed developer/review runs that have a PR, then
    synthesizes actionable handoff buttons from the PR's review state.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        async with await get_session() as session:
            async with session.begin():
                stmt = (
                    select(TaskRun)
                    .where(
                        TaskRun.status == "done",
                        TaskRun.role.in_(["developer", "command:review-pr"]),
                        TaskRun.pr_number.isnot(None),
                        func.coalesce(TaskRun.ended_at, TaskRun.started_at) >= cutoff,
                    )
                    .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
                    .limit(2)
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

        for run in runs:
            if not run.issue_number or run.pr_number is None:
                continue
            actions = await synthesize_pr_actions(run.issue_number)
            if actions:
                return {
                    "source": "pr-review-state",
                    "status": "awaiting_action",
                    "issue": run.issue_number,
                    "pr_number": run.pr_number,
                    "summary": "Actions synthesized from PR review state",
                    "next_actions": actions,
                }
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("synthesized_handoff.failed", exc_info=True)

    return None


# Network outage self-heal -----------------------------------------------------

# Roles with no checkpoint to resume from: a `command:*` run is a fresh
# `claude -p` slash-command conversation, and the supervisor's next poll
# re-proposes the action anyway, so retrying one here is redundant mechanism.
# `reviewer` is excluded too: it is a non-pipeline role with no `--resume`
# checkpoint of its own, and auto-resuming a failed review risks posting a
# stale verdict rather than a fresh one against the PR's current head.
_SELF_HEAL_ROLES = frozenset({"developer", "researcher", "planner"})
_SELF_HEAL_STATUSES = frozenset({"failed", "interrupted"})


async def attempt_network_self_heal(project_dir: Path | None = None) -> list[dict]:
    """Resume runs that failed because of a network outage, once it has cleared.

    Deliberately narrow. The general-purpose auto-retry this codebase used to
    have was removed for being unpredictable (migration
    ``020_drop_task_run_retry_columns``); the difference here is that
    eligibility requires the persisted error text to *positively* identify a
    transport failure. Overlapping the outage window is only a filter, never
    evidence, so an unrelated bug that happened to fail during an outage is
    never resumed.

    Resume rather than respawn: ``sova run --resume`` skips already-completed
    steps, which is the whole point (the incident that motivated this lost a
    14-minute, $5.65 developer run that failed at its final ``git push``). A run
    that completed no steps degenerates to a fresh spawn on its own, so there is
    one code path and no classifier deciding between them.

    Returns the resumes it started, for logging and tests.
    """
    try:
        from sova.config.loader import load_config
        from sova.supervisor.network_health import get_connectivity_tracker

        cfg = load_config(project_dir)
        guard = cfg.network_guard
        if not guard.enabled or not guard.auto_resume:
            return []

        tracker = get_connectivity_tracker()
        if tracker.is_down():
            return []
        # Flap guard: a connection that has only just answered may be about to
        # drop again, and spending the retry budget on it is how an outage
        # exhausts the cap without ever shipping anything.
        healthy_for = tracker.healthy_for_seconds()
        if healthy_for < guard.recovery_grace_seconds:
            log.debug("self_heal.grace_not_met", healthy_for=round(healthy_for))
            return []

        candidates = await _find_self_heal_candidates(project_dir, guard.resume_window_minutes)
        if not candidates:
            return []

        if guard.max_auto_resumes_per_hour:
            recent = await _count_recent_resumes(project_dir)
            if recent >= guard.max_auto_resumes_per_hour:
                log.info("self_heal.hourly_cap_reached", recent=recent, cap=guard.max_auto_resumes_per_hour)
                return []

        # One per tick. The 5-minute recovery loop is the rate limiter, so a
        # multi-project outage recovers steadily instead of spawning a herd into
        # the slot limit the moment the connection returns.
        return await _resume_one(candidates[0], project_dir)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("self_heal.failed", exc_info=True)
        return []


async def _find_self_heal_candidates(project_dir: Path | None, window_minutes: int) -> list[dict]:
    """Return resumable outage-failed runs, most valuable first.

    One candidate per issue (the newest), skipping any issue that already has a
    newer run which is running or done, and any run that has already been
    resumed once.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session
    from sova.utils.network import looks_like_network_outage

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    candidates: list[dict] = []

    async with await get_session(project_dir=project_dir) as session:
        async with session.begin():
            stmt = (
                select(TaskRun)
                .where(
                    TaskRun.status.in_(_SELF_HEAL_STATUSES),
                    # Stronger than status alone: excludes a deliberate stop, a
                    # watchdog kill and an external signal, none of which should
                    # be undone by an automatic resume.
                    TaskRun.termination_reason.is_(None),
                    TaskRun.role.in_(_SELF_HEAL_ROLES),
                    func.coalesce(TaskRun.ended_at, TaskRun.started_at) >= cutoff,
                )
                .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
            )
            runs = list((await session.execute(stmt)).scalars().all())

            seen_issues: set[str] = set()
            for run in runs:
                # Marked before any eligibility check, not after: `runs` is
                # ordered newest-first across all issues, so the first row
                # seen for an issue is always its newest matching run. If that
                # newest run is ineligible (not an outage, or itself a resume),
                # the issue must be skipped entirely rather than falling
                # through to an older run, which would resume stale work out
                # from under a more recent attempt.
                issue = run.issue_number or ""
                if not issue or issue in seen_issues:
                    continue
                seen_issues.add(issue)

                if not looks_like_network_outage(run.error_message):
                    continue
                # A run that is itself a resume is never resumed again. This
                # caps every chain at one automatic follow-up without needing a
                # column to tell automatic resumes from human ones: if the
                # retry also died, a flapping connection would otherwise keep
                # spending the budget, and handing back to a human at that
                # point is the safer default given this codebase removed its
                # general auto-retry for being unpredictable.
                if run.resumed_from_id is not None:
                    continue

                if await _has_newer_live_run(session, run):
                    continue
                if await _already_resumed(session, run.id):
                    continue

                steps_done = await session.scalar(
                    select(func.count(StepExecution.id)).where(
                        StepExecution.task_run_id == run.id,
                        StepExecution.status.in_(("passed", "done")),
                    )
                )
                candidates.append(
                    {
                        "run_id": run.id,
                        "issue": issue,
                        "role": run.role,
                        "pr_number": run.pr_number,
                        "steps_done": int(steps_done or 0),
                        "cost": run.total_cost_usd if run.total_cost_usd is not None else Decimal("0"),
                    }
                )

    # Most recovered work first, so the run that is nearly finished goes before
    # one that did nothing if the slot limit only allows a single resume.
    candidates.sort(key=lambda c: (c["steps_done"], c["cost"]), reverse=True)
    return candidates


async def _has_newer_live_run(session: AsyncSession, run: object) -> bool:
    """True when this issue already has any later run, regardless of its status.

    This is what stops a resume from duplicating work the system already redid
    on its own: in the motivating incident the supervisor respawned both issues
    four minutes later, which makes every earlier candidate for those issues
    pure waste (and a potential duplicate PR). Deliberately unfiltered by
    status: `seen_issues` already ensures only the single newest run matching
    the outer query's SQL filters reaches this check, so a hit here can only
    come from a newer row the outer query excluded for an unrelated reason
    (role, time window, termination_reason, or a status outside
    `_SELF_HEAL_STATUSES`), a case this function must still catch.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun

    newer = await session.scalar(
        select(func.count(TaskRun.id)).where(
            TaskRun.issue_number == run.issue_number,
            TaskRun.id > run.id,
        )
    )
    return bool(newer)


async def _already_resumed(session: AsyncSession, run_id: int) -> bool:
    """True when some run already resumed this one.

    ``resumed_from_id`` is written by WorkflowEngine for every ``--resume``
    spawn, so it marks "already handled" with no new column (the retry-tracking
    columns a previous design used were dropped in migration 020 and are
    deliberately not being re-added). A human-initiated resume sets it too,
    which is correct: it also means this run has been dealt with.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun

    existing = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.resumed_from_id == run_id))
    return bool(existing)


async def _count_recent_resumes(project_dir: Path | None) -> int:
    """Count resumes started in the last hour, as the per-project rate cap.

    Counts every resume rather than only automatic ones: a human already
    retrying this project repeatedly is exactly when an automatic resume should
    hold off, and it needs no marker column to distinguish them.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    async with await get_session(project_dir=project_dir) as session:
        async with session.begin():
            count = await session.scalar(
                select(func.count(TaskRun.id)).where(
                    TaskRun.resumed_from_id.isnot(None),
                    TaskRun.started_at >= cutoff,
                )
            )
    return int(count or 0)


async def _resume_one(candidate: dict, project_dir: Path | None) -> list[dict]:
    """Resume a single candidate through the normal spawn path.

    ``attempt_network_self_heal()`` runs from the background
    ``_periodic_recovery_loop``, not a request context, so the per-request
    ``get_project_slug()`` contextvar ``start_agent()`` would otherwise fall
    back to is never set there. In multi-project mode that left every resume
    landing on the unpopulated ``_DEFAULT_SLUG`` pool regardless of which
    project ``candidate`` actually came from. Resolving the slug from
    ``project_dir`` explicitly mirrors ``progression.py:execute_decision()``,
    which resolves the same way for the same reason. A ``None`` result (no
    ``project_dir``, or a single-project-mode directory that was never
    registered) falls through to ``start_agent()``'s existing default
    resolution, which is correct there since that pool's project_dir is
    pre-seeded by ``set_project_dir()``.
    """
    from sova.config.registry import find_slug_for_path
    from sova.dashboard.services.agent_lifecycle import start_agent

    slug = find_slug_for_path(project_dir) if project_dir else None

    # force=False on purpose: the memory, connectivity, slot and issue-conflict
    # checks all still apply. An automatic resume is the last thing that should
    # be allowed to bypass them.
    result = await start_agent(
        candidate["issue"],
        role=candidate["role"],
        resume_run_id=candidate["run_id"],
        pr_number=candidate["pr_number"],
        slug=slug,
    )

    if result.get("error"):
        log.warning(
            "self_heal.resume_rejected",
            run_id=candidate["run_id"],
            issue=candidate["issue"],
            error=result["error"],
        )
        return []

    log.info(
        "self_heal.resumed",
        run_id=candidate["run_id"],
        new_run_id=result.get("run_id"),
        issue=candidate["issue"],
        role=candidate["role"],
        steps_done=candidate["steps_done"],
    )
    _emit_self_heal_event(candidate)
    return [{**candidate, "new_run_id": result.get("run_id")}]


def _emit_self_heal_event(candidate: dict) -> None:
    try:
        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        skipped = f" Skipping {candidate['steps_done']} completed step(s)." if candidate["steps_done"] else ""
        emit_safe(
            f"#{candidate['issue']} {candidate['role']} resumed after network recovery",
            severity=FeedEventSeverity.info,
            detail=f"The previous run failed while the connection was down.{skipped}",
            category="connectivity",
        )
    except Exception:  # noqa: BLE001 (feed emission must never break the resume it reports)
        log.debug("self_heal.emit_failed", exc_info=True)

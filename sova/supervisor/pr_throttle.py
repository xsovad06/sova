"""PR creation throttle service -- queues PR creation behind CodeRabbit quota.

When ``coderabbit_quota.enabled``, ``CreatePRStep`` enqueues PR data instead
of creating immediately.  A background ``process_queue()`` loop drains entries
one at a time, respecting the quota window.  When disabled, the step creates
PRs directly (zero behavior change).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.models import CodeRabbitQuotaConfig
from sova.db.models import PRCreationQueue, PRQueueStatus
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.pr_throttle")

_MAX_QUEUE_AGE_HOURS = 2
_PROCESS_INTERVAL_SECONDS = 60
_POLL_INTERVAL_SECONDS = 30


async def enqueue(
    session: AsyncSession,
    *,
    task_run_id: int,
    issue_number: str | None,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
    repo: str = "",
    github_user: str = "",
    project_slug: str = "",
) -> int:
    """Add a PR creation request to the queue. Returns the queue entry ID."""
    entry = PRCreationQueue(
        task_run_id=task_run_id,
        issue_number=issue_number,
        title=title,
        body=body,
        base_branch=base_branch,
        head_branch=head_branch,
        repo=repo,
        github_user=github_user,
        status=PRQueueStatus.PENDING,
        project_slug=project_slug,
    )
    session.add(entry)
    await session.flush()
    entry_id = entry.id
    log.info("pr_throttle.enqueued", entry_id=entry_id, task_run_id=task_run_id, issue=issue_number or "(none)")
    return entry_id


async def dequeue(session: AsyncSession, *, task_run_id: int) -> bool:
    """Cancel a pending queue entry for a task run.

    Returns True if a pending entry was cancelled, False if none found
    or the entry was already processed.
    """
    stmt = select(PRCreationQueue).where(
        PRCreationQueue.task_run_id == task_run_id,
        PRCreationQueue.status == PRQueueStatus.PENDING,
    )
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry is None:
        return False
    entry.status = PRQueueStatus.CANCELLED
    entry.processed_at = datetime.now(timezone.utc)
    log.info("pr_throttle.dequeued", entry_id=entry.id, task_run_id=task_run_id)
    return True


async def get_queue_entry_status(
    session: AsyncSession,
    entry_id: int,
) -> dict | None:
    """Get the current status of a queue entry. Returns None if not found."""
    entry = await session.get(PRCreationQueue, entry_id)
    if entry is None:
        return None
    return {
        "id": entry.id,
        "status": entry.status,
        "pr_number": entry.pr_number,
        "pr_url": entry.pr_url,
        "error_message": entry.error_message,
    }


async def poll_until_created(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    entry_id: int,
    *,
    timeout_seconds: float = 3600,
) -> dict | None:
    """Poll a queue entry until it reaches a terminal status.

    Returns the entry status dict, or None on timeout.
    Used by CreatePRStep to wait for the background processor.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        async with await session_factory() as session:
            status = await get_queue_entry_status(session, entry_id)
        if status is None:
            return None
        if status["status"] in (PRQueueStatus.CREATED, PRQueueStatus.FAILED, PRQueueStatus.CANCELLED):
            return status
    # Timed out -- mark as failed so the entry doesn't linger
    async with await session_factory() as session:
        async with session.begin():
            entry = await session.get(PRCreationQueue, entry_id)
            if entry and entry.status in (PRQueueStatus.PENDING, PRQueueStatus.CREATING):
                entry.status = PRQueueStatus.FAILED
                entry.error_message = "Timed out waiting for PR creation"
                entry.processed_at = datetime.now(timezone.utc)
    return None


async def process_queue(
    session: AsyncSession,
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
    project_dir: Path | None = None,
) -> int:
    """Process one pending queue entry if quota allows.

    Returns the number of entries processed (0 or 1).
    Called periodically by the background task.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=_MAX_QUEUE_AGE_HOURS)

    # Phase 1: cancel stale entries, check quota, select entry, mark CREATING
    async with session.begin():
        # Cancel stale entries (edge case 6)
        stale_stmt = select(PRCreationQueue).where(
            PRCreationQueue.status == PRQueueStatus.PENDING,
            PRCreationQueue.enqueued_at < stale_cutoff,
            PRCreationQueue.project_slug == project_slug,
        )
        stale_result = await session.execute(stale_stmt)
        for stale_entry in stale_result.scalars().all():
            stale_entry.status = PRQueueStatus.CANCELLED
            stale_entry.processed_at = now
            stale_entry.error_message = "Cancelled: exceeded max queue age"
            log.warning("pr_throttle.stale_cancelled", entry_id=stale_entry.id)

        # Check quota
        from sova.supervisor.coderabbit_quota import get_quota_status

        quota = await get_quota_status(session, config, project_slug=project_slug)
        if not quota.can_create_pr:
            log.debug("pr_throttle.quota_exhausted", next_minutes=quota.next_available_minutes)
            return 0

        # Get the oldest pending entry (FIFO)
        pending_stmt = (
            select(PRCreationQueue)
            .where(
                PRCreationQueue.status == PRQueueStatus.PENDING,
                PRCreationQueue.project_slug == project_slug,
            )
            .order_by(PRCreationQueue.enqueued_at)
            .limit(1)
        )
        result = await session.execute(pending_stmt)
        entry = result.scalar_one_or_none()
        if entry is None:
            return 0

        # Mark as CREATING and commit durably BEFORE network call (edge case 9).
        # This prevents a crash during create_pr from rolling back to PENDING.
        entry.status = PRQueueStatus.CREATING
        entry_id = entry.id
        entry_task_run_id = entry.task_run_id
        entry_title = entry.title
        entry_body = entry.body
        entry_base_branch = entry.base_branch
        entry_head_branch = entry.head_branch
        entry_repo = entry.repo
        entry_github_user = entry.github_user
        entry_issue_number = entry.issue_number

    # Create the PR (outside the transaction -- CREATING is already durable)
    from sova.git import operations as git_ops

    try:
        pr_info = await git_ops.create_pr(
            title=entry_title,
            body=entry_body,
            base=entry_base_branch,
            head=entry_head_branch,
            repo=entry_repo,
        )
    except Exception as exc:
        async with session.begin():
            entry = await session.get(PRCreationQueue, entry_id)
            if entry is not None:
                entry.status = PRQueueStatus.FAILED
                entry.error_message = str(exc)
                entry.processed_at = datetime.now(timezone.utc)
        log.error("pr_throttle.create_failed", entry_id=entry_id, error=str(exc), exc_info=True)
        return 0

    # PR created successfully -- persist results
    async with session.begin():
        entry = await session.get(PRCreationQueue, entry_id)
        if entry is not None:
            entry.status = PRQueueStatus.CREATED
            entry.pr_number = pr_info.number
            entry.pr_url = pr_info.url
            entry.processed_at = datetime.now(timezone.utc)

        # Record the CodeRabbit event (edge case 7)
        from sova.db.models import CodeRabbitEvent

        session.add(
            CodeRabbitEvent(
                pr_number=pr_info.number,
                event_type="pr_created",
                review_id=f"pr-{pr_info.number}-created",
                recorded_at=datetime.now(timezone.utc),
                project_slug=project_slug,
            )
        )

        # Update the TaskRun with PR info
        from sova.db.models import TaskRun

        task_run = await session.get(TaskRun, entry_task_run_id)
        if task_run is not None:
            task_run.pr_number = pr_info.number

    # Run side effects (edge case 8) -- outside transaction
    await run_post_create_side_effects(
        pr_number=pr_info.number,
        issue_number=entry_issue_number,
        repo=entry_repo,
        github_user=entry_github_user,
        project_dir=project_dir,
    )

    log.info(
        "pr_throttle.created",
        entry_id=entry_id,
        pr=pr_info.number,
        issue=entry_issue_number or "(none)",
    )
    return 1


async def run_post_create_side_effects(
    *,
    pr_number: int,
    issue_number: str | None,
    repo: str,
    github_user: str,
    project_dir: Path | None = None,
) -> None:
    """Run PR assignment and issue state transition after creation.

    Extracted from CreatePRStep._post_create_side_effects for reuse
    by process_queue without needing an ExecutionContext.

    ``project_dir`` is required for adapter creation (config loading).
    Callers in background tasks must pass it explicitly since the
    per-request context variable is not set outside request handlers.
    """
    from sova.adapters.base import TaskState
    from sova.git import operations as git_ops

    if github_user:
        try:
            await git_ops.assign_pr(
                pr_number,
                assignee=github_user,
                repo=repo,
                github_user=github_user,
            )
        except Exception:
            log.warning("pr_throttle.assign_failed", pr=pr_number, exc_info=True)

    if issue_number:
        try:
            from sova.adapters import create_adapter
            from sova.config.loader import load_config

            cfg = load_config(project_dir)
            adapter = create_adapter(cfg)
            await adapter.transition_state(issue_number, TaskState.IN_REVIEW)
        except Exception:
            log.warning("pr_throttle.tracker_update_failed", pr=pr_number, exc_info=True)

    await _trigger_coderabbit_review(pr_number=pr_number, repo=repo, github_user=github_user, project_dir=project_dir)


async def _trigger_coderabbit_review(
    *,
    pr_number: int,
    repo: str,
    github_user: str,
    project_dir: Path | None = None,
) -> None:
    """Post @coderabbitai review comment when trigger_review is enabled."""
    from sova.config.loader import load_config

    cfg = load_config(project_dir)
    if not cfg.external_reviews.coderabbit.trigger_review:
        return

    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    log.info("pr_throttle.trigger_coderabbit", pr=pr_number)
    try:
        env = await resolve_gh_env(github_user) if github_user else None
    except Exception:
        log.warning("pr_throttle.trigger_coderabbit_gh_env_failed", pr=pr_number, exc_info=True)
        return
    result = await run(
        "gh",
        "pr",
        "comment",
        str(pr_number),
        "--repo",
        repo,
        "--body",
        "@coderabbitai review",
        env=env,
    )
    from sova.supervisor.github_quota import track_rate_limit

    track_rate_limit(result, github_user or "")
    if not result.success:
        log.warning("pr_throttle.trigger_coderabbit_failed", pr=pr_number, stderr=result.stderr[:200])


async def recover_creating_entries(session: AsyncSession) -> int:
    """Reset entries stuck in 'creating' status back to 'pending'.

    Called on startup to recover from server crashes mid-creation (edge case 9).
    Safe because PR dedup in _try_adopt_existing_pr prevents double creation.
    """
    stmt = (
        update(PRCreationQueue)
        .where(PRCreationQueue.status == PRQueueStatus.CREATING)
        .values(status=PRQueueStatus.PENDING)
    )
    result = await session.execute(stmt)
    count = result.rowcount
    if count:
        log.info("pr_throttle.recovered_creating", count=count)
    return count


async def process_queue_loop(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
    project_dir: Path | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that periodically processes the PR creation queue.

    Runs until stop_event is set or the task is cancelled.
    """
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            async with await session_factory() as session:
                await process_queue(session, config, project_slug=project_slug, project_dir=project_dir)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("pr_throttle.loop_error", exc_info=True)
        await asyncio.sleep(_PROCESS_INTERVAL_SECONDS)

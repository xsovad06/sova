"""PR creation throttle service: queues PR creation behind CodeRabbit quota.

When ``coderabbit_quota.enabled``, ``CreatePRStep`` enqueues PR data instead
of creating immediately.  A background ``process_queue()`` loop drains entries
one at a time, respecting the quota window.  When disabled, the step creates
PRs directly (zero behavior change).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.models import CodeRabbitQuotaConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import PRCreationQueue, PRQueueStatus, TaskRun
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger
from sova.utils.process import is_process_alive

log = get_logger(component="supervisor.pr_throttle")

_MAX_QUEUE_AGE_HOURS = 2
_PROCESS_INTERVAL_SECONDS = 60
_POLL_INTERVAL_SECONDS = 30

# A loop touches its heartbeat before and after every cycle, and every interval while a queue pass runs,
# so the file is never older than about one interval however long ``gh pr create`` takes. Five intervals
# leaves room for a delayed write.
_HEARTBEAT_PREFIX = "pr-throttle."
_HEARTBEAT_SUFFIX = ".heartbeat"
_HEARTBEAT_MAX_AGE_SECONDS = 5 * _PROCESS_INTERVAL_SECONDS

_RUN_ENDED_REASON = "Cancelled: the run ended before the PR was created"


def heartbeat_path(project_dir: Path | str, pid: int | None = None) -> Path:
    """Path of the file *pid*'s (default: this process's) throttle loop touches to show it is alive.

    One file per process, named for its pid: processes serving one project (a ``make dev`` dashboard next
    to the daemon) never write or delete each other's file, so neither can hide the other.
    """
    name = f"{_HEARTBEAT_PREFIX}{os.getpid() if pid is None else pid}{_HEARTBEAT_SUFFIX}"
    return Path(project_dir) / ".claude" / "agent-control" / name


def _heartbeat_files(project_dir: Path | str) -> list[tuple[str, Path]]:
    """Every process's heartbeat file for *project_dir* as ``(pid text, path)``. Empty if the glob fails."""
    directory = heartbeat_path(project_dir).parent
    try:
        paths = sorted(directory.glob(f"{_HEARTBEAT_PREFIX}*{_HEARTBEAT_SUFFIX}"))
    except OSError:
        return []
    return [(p.name[len(_HEARTBEAT_PREFIX) : -len(_HEARTBEAT_SUFFIX)], p) for p in paths]


def _fresh_heartbeat_pids(project_dir: Path | str) -> list[str]:
    """The pid text of every heartbeat file touched within the maximum age."""
    now = time.time()
    fresh = []
    for pid_text, path in _heartbeat_files(project_dir):
        try:
            if now - path.stat().st_mtime <= _HEARTBEAT_MAX_AGE_SECONDS:
                fresh.append(pid_text)
        except OSError:
            continue
    return fresh


def _prune_stale_heartbeats(project_dir: Path | str) -> None:
    """Remove heartbeat files older than the maximum age, left behind by processes that crashed. Never raises.

    A stale file never reads as alive, so removing it changes no answer; it only stops one file per
    crashed process from piling up. A live process rewrites its own file every interval, long before it
    could reach the maximum age.
    """
    now = time.time()
    for _pid_text, path in _heartbeat_files(project_dir):
        try:
            if now - path.stat().st_mtime > _HEARTBEAT_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            log.debug("pr_throttle.heartbeat_prune_failed", path=str(path), exc_info=True)


def touch_heartbeat(project_dir: Path | str, owner: str = "") -> None:
    """Record that this process's throttle loop is serving *project_dir*, stamping *owner*. Never raises.

    Only the mtime says "alive" (``throttle_loop_alive``); the content names the loop that wrote, so
    ``clear_heartbeat`` can tell its own beat from another loop's in this process. A failed write only
    costs a spurious direct PR creation in ``CreatePRStep``, so it is logged and swallowed rather than
    allowed to end the loop.
    """
    path = heartbeat_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(owner, encoding="utf-8")
    except OSError:
        log.warning("pr_throttle.heartbeat_write_failed", path=str(path), exc_info=True)
        return
    _prune_stale_heartbeats(project_dir)


def clear_heartbeat(project_dir: Path | str, owner: str = "") -> None:
    """Remove this process's heartbeat for *project_dir* when its loop stops. Never raises.

    Without this a stopped loop keeps reading as alive until the file ages out, and every ``CreatePRStep``
    in that window enqueues onto a queue nobody drains instead of taking the direct-creation path.

    Only this process's own file is touched, so a sibling process serving the project (a ``make dev``
    dashboard next to the daemon) keeps reading as alive and there is no cross-process check-then-unlink
    to race. The file is unlinked only while it still holds *owner*, so a loop that stops after another
    loop in this process has started beating does not remove the newer one's heartbeat. The check and
    the unlink run with no ``await`` between them, so no other task in this process interleaves.
    """
    path = heartbeat_path(project_dir)
    if read_text_or_none(path) != owner:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("pr_throttle.heartbeat_clear_failed", path=str(path), exc_info=True)


def throttle_loop_alive(project_dir: Path | str) -> bool:
    """Whether any process's throttle loop touched *project_dir*'s heartbeat recently.

    ``CreatePRStep`` runs in a separate process from the daemon that owns the loop, so the files' mtimes
    are the only signal it can read. A missing, stale or unreadable heartbeat reads as not alive, which
    sends the step down the direct-creation path instead of onto a queue nobody drains.
    """
    return bool(_fresh_heartbeat_pids(project_dir))


def sibling_loop_alive(project_dir: Path | str) -> bool:
    """Whether a throttle loop in another live process is serving *project_dir*.

    A fresh heartbeat alone cannot tell a sibling (a ``make dev`` dashboard next to the daemon) from a
    daemon that crashed seconds ago: both leave a file younger than the maximum age. Each process has its
    own file named for its pid, so every fresh file is judged on its own: one counts while its process
    still exists and is not this one, whatever any other process wrote last. A fresh file whose pid
    cannot be parsed is treated as a live sibling, the direction that cannot reset an entry under an
    in-flight ``gh pr create``.

    A pid recycled by an unrelated process within the maximum age reads as a sibling; the cost is that
    startup recovery is skipped once, leaving a crashed run's ``creating`` entry as it was.
    """
    for pid_text in _fresh_heartbeat_pids(project_dir):
        try:
            pid = int(pid_text)
        except ValueError:
            return True
        if pid != os.getpid() and is_process_alive(pid):
            return True
    return False


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


async def dequeue(session: AsyncSession, *, task_run_id: int, reason: str | None = None) -> bool:
    """Cancel every pending queue entry for a task run.

    A retried step enqueues once per attempt, so one run can hold several pending entries. An entry
    already past ``pending`` (``creating`` has a ``gh pr create`` in flight) is left alone.

    Returns True if at least one pending entry was cancelled, False if none found
    or every entry was already processed.

    One conditional UPDATE, not select-then-set: a ``process_queue()`` that flipped an entry to
    ``creating`` between the two would otherwise have that state overwritten with ``cancelled``.
    """
    values: dict = {"status": PRQueueStatus.CANCELLED, "processed_at": datetime.now(timezone.utc)}
    if reason is not None:
        values["error_message"] = reason
    result = await session.execute(
        update(PRCreationQueue)
        .where(
            PRCreationQueue.task_run_id == task_run_id,
            PRCreationQueue.status == PRQueueStatus.PENDING,
        )
        .values(**values)
        .returning(PRCreationQueue.id)
    )
    entry_ids = result.scalars().all()
    for entry_id in entry_ids:
        log.info("pr_throttle.dequeued", entry_id=entry_id, task_run_id=task_run_id)
    return bool(entry_ids)


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


class QueueEntryMissingError(LookupError):
    """A queue entry being polled no longer exists, which is not the same outcome as a timeout."""

    def __init__(self, entry_id: int) -> None:
        super().__init__(f"queue entry {entry_id} no longer exists")
        self.entry_id = entry_id


async def poll_until_created(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    entry_id: int,
    *,
    timeout_seconds: float = 3600,
) -> dict | None:
    """Poll a queue entry until it reaches a terminal status.

    Returns the entry status dict, or None on timeout. Raises ``QueueEntryMissingError`` if the entry
    row is gone, so a caller can tell that from a wait that merely ran out.
    Used by CreatePRStep to wait for the background processor. A timeout leaves
    the entry untouched: the caller cancels it, because it must do so on every
    other way out of the wait too (step deadline, cancellation), and a
    ``creating`` entry must not be marked failed under a ``gh pr create`` that
    may still succeed.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # Never sleep past the deadline: the wait is sized to leave the caller time to clean up.
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        async with await session_factory() as session:
            status = await get_queue_entry_status(session, entry_id)
        if status is None:
            raise QueueEntryMissingError(entry_id)
        if status["status"] in (PRQueueStatus.CREATED, PRQueueStatus.FAILED, PRQueueStatus.CANCELLED):
            return status
    return None


def _ended_run_ids() -> Select:
    """Subquery of the task runs that have ended (``TASK_RUN_TERMINAL``).

    A queue entry whose run row is missing matches nothing here: only an observed terminal status
    cancels an entry, so a deleted run does not strand or discard one.
    """
    return select(TaskRun.id).where(TaskRun.status.in_(TASK_RUN_TERMINAL))


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

        # Cancel entries whose run has ended. The step's own cleanup and the run-finalization backstop each
        # miss some (a SIGKILLed `sova run` is only swept by liveness recovery, which never dequeues), so
        # the loop is the authority: a held PR is never created once its run is over.
        ended_stmt = select(PRCreationQueue).where(
            PRCreationQueue.status == PRQueueStatus.PENDING,
            PRCreationQueue.project_slug == project_slug,
            PRCreationQueue.task_run_id.in_(_ended_run_ids()),
        )
        ended_result = await session.execute(ended_stmt)
        for ended_entry in ended_result.scalars().all():
            ended_entry.status = PRQueueStatus.CANCELLED
            ended_entry.processed_at = now
            ended_entry.error_message = _RUN_ENDED_REASON
            log.warning("pr_throttle.run_ended_cancelled", entry_id=ended_entry.id, task_run_id=ended_entry.task_run_id)

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
        # Conditional on still being PENDING and its run still live: the run's own step may have cancelled
        # the entry since the SELECT above, and the run may have ended after the ended-run sweep, whose
        # finalization backstop skips an entry already past pending. Neither may become a PR.
        flipped = await session.execute(
            update(PRCreationQueue)
            .where(
                PRCreationQueue.id == entry.id,
                PRCreationQueue.status == PRQueueStatus.PENDING,
                ~PRCreationQueue.task_run_id.in_(_ended_run_ids()),
            )
            .values(status=PRQueueStatus.CREATING)
        )
        if flipped.rowcount != 1:
            log.info("pr_throttle.entry_cancelled_before_creation", entry_id=entry.id)
            return 0
        entry_id = entry.id
        entry_task_run_id = entry.task_run_id
        entry_title = entry.title
        entry_body = entry.body
        entry_base_branch = entry.base_branch
        entry_head_branch = entry.head_branch
        entry_repo = entry.repo
        entry_github_user = entry.github_user
        entry_issue_number = entry.issue_number

    # Create the PR (outside the transaction: CREATING is already durable)
    from sova.git import operations as git_ops

    try:
        pr_info = await git_ops.create_pr(
            title=entry_title,
            body=entry_body,
            base=entry_base_branch,
            head=entry_head_branch,
            repo=entry_repo,
            github_user=entry_github_user,
        )
    except (RuntimeError, OSError) as exc:
        async with session.begin():
            entry = await session.get(PRCreationQueue, entry_id)
            if entry is not None:
                entry.status = PRQueueStatus.FAILED
                entry.error_message = str(exc)
                entry.processed_at = datetime.now(timezone.utc)
        log.error("pr_throttle.create_failed", entry_id=entry_id, error=str(exc), exc_info=True)
        return 0

    # PR created successfully: persist results
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
        task_run = await session.get(TaskRun, entry_task_run_id)
        if task_run is not None:
            task_run.pr_number = pr_info.number

    # Run side effects (edge case 8): outside transaction
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
    """Run PR assignment, issue state transition, and LDAP reviewer suggestion after creation.

    Extracted from CreatePRStep._post_create_side_effects for reuse
    by process_queue without needing an ExecutionContext. Runs the same LDAP
    reviewer suggestion as the direct-creation path so queued PRs (throttled
    behind CodeRabbit quota) are not silently missing it.

    ``project_dir`` is required for adapter creation (config loading).
    Callers in background tasks must pass it explicitly since the
    per-request context variable is not set outside request handlers.
    """
    from sova.adapters.base import TaskState
    from sova.config.loader import load_config
    from sova.git import operations as git_ops

    if github_user:
        try:
            await git_ops.assign_pr(
                pr_number,
                assignee=github_user,
                repo=repo,
                github_user=github_user,
            )
        except (RuntimeError, OSError):
            log.warning("pr_throttle.assign_failed", pr=pr_number, exc_info=True)

    cfg = load_config(project_dir)
    adapter = None
    if issue_number:
        try:
            from sova.adapters import create_adapter

            adapter = create_adapter(cfg)
            await adapter.transition_state(issue_number, TaskState.IN_REVIEW)
        except Exception:  # noqa: BLE001 (config load, adapter construction and tracker call each fail differently)
            log.warning("pr_throttle.tracker_update_failed", pr=pr_number, exc_info=True)

    if cfg.ldap.enabled:
        if adapter is None:
            try:
                from sova.adapters import create_adapter

                adapter = create_adapter(cfg)
            except Exception:  # noqa: BLE001 (adapter construction fails differently per task source)
                log.warning("pr_throttle.ldap_adapter_create_failed", pr=pr_number, exc_info=True)
        if adapter is not None:
            from sova.core.steps.create_pr import suggest_ldap_reviewers

            await suggest_ldap_reviewers(
                cfg.ldap,
                adapter,
                github_user=github_user,
                issue_number=issue_number,
                pr_number=pr_number,
            )

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
    except (RuntimeError, OSError):
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
    """Reset entries stuck in 'creating' back to 'pending', unless their run has ended.

    Called on startup to recover from server crashes mid-creation (edge case 9).

    Only safe while no other process is draining this project's queue: ``process_queue`` calls
    ``create_pr`` directly, with no adoption of an existing PR, so resetting an entry whose ``gh pr create``
    is still running in a sibling process would open the PR a second time. Callers gate this on
    ``sibling_loop_alive()``.

    A ``creating`` entry is the one state neither the step's cleanup nor run finalization cancels (its
    ``gh pr create`` was in flight), so a run that ended in the meantime would otherwise have its entry
    reset and drained into a PR for a finished run. Those are cancelled instead.

    Returns the number of entries reset to ``pending``.
    """
    cancelled = await session.execute(
        update(PRCreationQueue)
        .where(PRCreationQueue.status == PRQueueStatus.CREATING, PRCreationQueue.task_run_id.in_(_ended_run_ids()))
        .values(
            status=PRQueueStatus.CANCELLED,
            processed_at=datetime.now(timezone.utc),
            error_message=_RUN_ENDED_REASON,
        )
    )
    if cancelled.rowcount:
        log.warning("pr_throttle.recovered_creating_run_ended", count=cancelled.rowcount)

    result = await session.execute(
        update(PRCreationQueue)
        .where(PRCreationQueue.status == PRQueueStatus.CREATING)
        .values(status=PRQueueStatus.PENDING)
    )
    count = result.rowcount
    if count:
        log.info("pr_throttle.recovered_creating", count=count)
    return count


class PRThrottleLoop:
    """Background loop that periodically processes the PR creation queue."""

    def __init__(
        self,
        session_factory: Callable[[], Awaitable[AsyncSession]],
        config: CodeRabbitQuotaConfig,
        *,
        project_slug: str = "",
        project_dir: Path | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._project_slug = project_slug
        self._project_dir = project_dir
        self._stop_event = stop_event
        self._wake_event = asyncio.Event()
        # Stamped into this process's heartbeat file so this loop only clears a heartbeat it wrote itself.
        self._heartbeat_owner = f"{os.getpid()}:{uuid.uuid4().hex}"

    def reload_config(self, config: CodeRabbitQuotaConfig) -> None:
        """Hot-reload config (called by settings router after TOML update)."""
        self._config = config
        self._wake_event.set()

    async def _interruptible_sleep(self, delay: float) -> None:
        """Sleep for *delay* seconds, but wake early if config is reloaded."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            self._wake_event.clear()

    def _beat(self) -> None:
        """Touch the heartbeat that tells ``CreatePRStep`` this project's queue is being drained."""
        if self._project_dir is not None:
            touch_heartbeat(self._project_dir, self._heartbeat_owner)

    async def _beat_while_processing(self) -> None:
        """Touch the heartbeat every interval until cancelled.

        A queue pass runs ``gh pr create`` and its side effects one after another, each allowed minutes,
        so a slow pass could outlast the heartbeat's maximum age and send a concurrent ``CreatePRStep``
        down the direct path past the quota.
        """
        while True:
            self._beat()
            await asyncio.sleep(_PROCESS_INTERVAL_SECONDS)

    async def run(self) -> None:
        """Main loop: process queue entries, then sleep between iterations.

        The heartbeat is removed on every way out (stop event, cancellation at shutdown), so a stopped
        loop stops reading as alive and ``CreatePRStep`` falls back to direct creation immediately, unless
        a sibling process serving the same project still beats its own file (see ``clear_heartbeat``).
        """
        try:
            while True:
                if self._stop_event and self._stop_event.is_set():
                    break
                beater = asyncio.create_task(self._beat_while_processing())
                try:
                    async with await self._session_factory() as session:
                        await process_queue(
                            session,
                            self._config,
                            project_slug=self._project_slug,
                            project_dir=self._project_dir,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 (background loop must survive any single-cycle error)
                    log.warning("pr_throttle.loop_error", exc_info=True)
                finally:
                    beater.cancel()
                self._beat()
                await self._interruptible_sleep(_PROCESS_INTERVAL_SECONDS)
        finally:
            if self._project_dir is not None:
                clear_heartbeat(self._project_dir, self._heartbeat_owner)

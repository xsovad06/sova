"""Merge queue monitor: background loop that polls GitHub merge queues.

Tracks PRs enqueued in merge queues via MergeQueueEntry DB records.
When a PR merges, triggers post-merge cleanup (branch deletion, issue
state transition, worktree removal). When a PR is ejected, emits a
feed event and sends a desktop notification.

Entry creation happens in two places:
  1. agent_recovery.py: when recover_stale_runs detects a dead merge-role
     agent with a PR still in the queue
  2. agent_lifecycle.py: when _wait_and_finalize detects a merge-role
     agent exiting with a PR still in the queue
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sova.config.models import IntegrationConfig
from sova.utils.logging import get_logger

log = get_logger(component="merge_queue_monitor")


@dataclass
class MergeQueueMonitor:
    """Polls MergeQueueEntry records and triggers post-merge cleanup."""

    project_dir: Path
    repo: str
    github_user: str
    integration_config: IntegrationConfig
    notification_config: Any = None

    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _reload_event: asyncio.Event = field(default_factory=asyncio.Event)

    def reload_config(
        self,
        integration_config: IntegrationConfig,
        notification_config: Any = None,
    ) -> None:
        """Hot-reload config (called by settings router after TOML update)."""
        self.integration_config = integration_config
        if notification_config is not None:
            self.notification_config = notification_config
        self._reload_event.set()

    async def _interruptible_sleep(self, delay: float) -> bool:
        """Sleep for *delay* seconds, waking early on config reload or stop.

        Returns True if the loop should exit (stop event set), False otherwise.
        """
        stop_fut = asyncio.ensure_future(self._stop_event.wait())
        reload_fut = asyncio.ensure_future(self._reload_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_fut, reload_fut},
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_fut in done:
                return True
            if reload_fut in done:
                self._reload_event.clear()
            return False
        finally:
            stop_fut.cancel()
            reload_fut.cancel()

    async def run_loop(self) -> None:
        """Main polling loop. Runs until cancelled or stopped."""
        interval = self.integration_config.merge_queue_poll_interval
        log.info("merge_queue_monitor.started", poll_interval=interval, repo=self.repo)
        while not self._stop_event.is_set():
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("merge_queue_monitor.cycle_error", exc_info=True)
            interval = self.integration_config.merge_queue_poll_interval
            should_stop = await self._interruptible_sleep(interval)
            if should_stop:
                break

    def stop(self) -> None:
        self._stop_event.set()

    async def _poll_cycle(self) -> None:
        """Single poll cycle: check all queued entries."""
        from sova.supervisor.github_quota import get_github_quota_tracker

        tracker = get_github_quota_tracker(self.github_user)
        if tracker.should_skip():
            log.info("merge_queue_monitor.skipped_rate_limited")
            return

        entries = await _load_queued_entries(self.project_dir)
        if not entries:
            return

        for entry in entries:
            try:
                await self._check_entry(entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning(
                    "merge_queue_monitor.entry_error",
                    entry_id=entry["id"],
                    pr=entry["pr_number"],
                    exc_info=True,
                )

    async def _check_entry(self, entry: dict) -> None:
        """Check a single merge queue entry and act on its status."""
        from sova.git.merge import get_merge_queue_status

        pr_number = entry["pr_number"]
        repo = entry["repo"]
        github_user = entry.get("github_user") or self.github_user

        status = await get_merge_queue_status(pr_number, repo=repo, github_user=github_user)

        if status.is_merged:
            await self._handle_merged(entry)
        elif status.is_failed:
            await self._handle_ejected(entry, status.state)
        elif not status.in_queue and status.state == "NOT_QUEUED":
            merged = await self._check_pr_merged_directly(pr_number, repo, github_user)
            if merged:
                await self._handle_merged(entry)
            else:
                await self._handle_ejected(entry, "NOT_QUEUED")
        else:
            timeout = self.integration_config.merge_queue_timeout
            enqueued_at = entry["enqueued_at"]
            if enqueued_at.tzinfo is None:
                enqueued_at = enqueued_at.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - enqueued_at).total_seconds()
            if elapsed > timeout:
                await self._handle_timeout(entry)
            else:
                position_info = f" (position {status.position})" if status.position is not None else ""
                log.info(
                    "merge_queue_monitor.still_queued",
                    pr=pr_number,
                    state=status.state,
                    position=position_info,
                    elapsed_s=int(elapsed),
                )

    async def _handle_merged(self, entry: dict) -> None:
        """PR merged: run post-merge cleanup and update entry."""
        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        pr_number = entry["pr_number"]
        repo = entry["repo"]
        issue_number = entry.get("issue_number")
        branch_name = entry.get("branch_name", "")
        github_user = entry.get("github_user") or self.github_user

        log.info("merge_queue_monitor.merged", pr=pr_number, repo=repo, issue=issue_number)

        cleanup_actions: list[str] = []

        if branch_name:
            try:
                from sova.git.merge import delete_remote_branch

                await delete_remote_branch(branch_name, repo=repo, github_user=github_user)
                cleanup_actions.append(f"branch {branch_name!r} deleted")
            except Exception:
                log.warning("merge_queue_monitor.branch_delete_failed", branch=branch_name, exc_info=True)

        if issue_number:
            try:
                await self._transition_issue_state(issue_number, repo, github_user)
                state = self.integration_config.post_merge_state
                cleanup_actions.append(f"issue #{issue_number} moved to {state}")
            except Exception:
                log.warning("merge_queue_monitor.state_transition_failed", issue=issue_number, exc_info=True)

        await self._cleanup_local_worktree(entry)

        await _update_entry_status(entry["id"], "merged", self.project_dir)

        detail = ", ".join(cleanup_actions) if cleanup_actions else None
        emit_safe(
            f"PR #{pr_number} merged from queue (cleanup complete)",
            severity=FeedEventSeverity.success,
            detail=detail,
            category="merge_queue",
            metadata={"pr_number": pr_number, "repo": repo, "issue": issue_number},
        )

        self._notify(
            subtitle=f"PR #{pr_number} merged",
            message=f"{repo}: post-merge cleanup complete",
        )

    async def _handle_ejected(self, entry: dict, state: str) -> None:
        """PR ejected from queue: update entry, notify."""
        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        pr_number = entry["pr_number"]
        repo = entry["repo"]

        log.warning("merge_queue_monitor.ejected", pr=pr_number, state=state, repo=repo)

        await _update_entry_status(entry["id"], "ejected", self.project_dir)

        emit_safe(
            f"PR #{pr_number} ejected from merge queue: {state}",
            severity=FeedEventSeverity.warning,
            detail=f"Manual re-enqueue required for {repo}",
            category="merge_queue",
            metadata={"pr_number": pr_number, "repo": repo, "state": state},
        )

        self._notify(
            subtitle=f"PR #{pr_number} ejected",
            message=f"{repo}: {state}",
        )

    async def _handle_timeout(self, entry: dict) -> None:
        """Queue timeout: update entry, notify."""
        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        pr_number = entry["pr_number"]
        repo = entry["repo"]
        timeout = self.integration_config.merge_queue_timeout

        log.warning("merge_queue_monitor.timeout", pr=pr_number, timeout=timeout, repo=repo)

        await _update_entry_status(entry["id"], "timeout", self.project_dir)

        emit_safe(
            f"PR #{pr_number} merge queue timed out after {timeout}s",
            severity=FeedEventSeverity.warning,
            detail=f"Check {repo} merge queue status manually",
            category="merge_queue",
            metadata={"pr_number": pr_number, "repo": repo, "timeout": timeout},
        )

        self._notify(
            subtitle=f"PR #{pr_number} queue timeout",
            message=f"{repo}: exceeded {timeout}s",
        )

    async def _transition_issue_state(self, issue_number: str, repo: str, github_user: str) -> None:
        """Transition issue state after merge."""
        from sova.adapters import create_adapter
        from sova.config.loader import load_config
        from sova.git.merge import handle_post_merge_state

        cfg = load_config(self.project_dir)
        adapter = create_adapter(
            cfg.task_source.type,
            repo=repo,
            github_user=github_user,
            project_number=cfg.task_source.github_project_number,
        )
        await handle_post_merge_state(
            issue_number,
            post_merge_state=self.integration_config.post_merge_state,
            repo=repo,
            github_user=github_user,
            adapter=adapter,
        )

    async def _cleanup_local_worktree(self, entry: dict) -> None:
        """Remove local worktree if it exists."""
        issue_number = entry.get("issue_number")
        if not issue_number:
            return

        worktree_path = self.project_dir / ".claude" / "worktrees" / str(issue_number)
        if not worktree_path.exists():
            return

        try:
            from sova.git.worktree import cleanup_worktree

            await cleanup_worktree(worktree_path, cwd=self.project_dir)
            log.info("merge_queue_monitor.worktree_cleaned", path=str(worktree_path))
        except Exception:
            log.warning("merge_queue_monitor.worktree_cleanup_failed", path=str(worktree_path), exc_info=True)

    async def _check_pr_merged_directly(self, pr_number: int, repo: str, github_user: str) -> bool:
        """Check if a PR was merged via a direct API check (not merge queue)."""
        try:
            from sova.git.pr import get_pr_status

            status = await get_pr_status(pr_number, repo=repo, github_user=github_user)
            return status.state == "MERGED"
        except Exception:
            log.debug("merge_queue_monitor.merged_check_failed", pr=pr_number, exc_info=True)
            return False

    def _notify(self, *, subtitle: str, message: str) -> None:
        """Send desktop notification if configured."""
        if self.notification_config is None:
            return
        try:
            from sova.ipc.notifications import notify

            notify(
                self.notification_config,
                title="SOVA",
                message=message,
                subtitle=subtitle,
                group="sova-merge-queue",
            )
        except Exception:
            log.debug("merge_queue_monitor.notify_failed", exc_info=True)


async def create_merge_queue_entry(
    *,
    pr_number: int,
    repo: str,
    project_dir: Path,
    issue_number: str | None = None,
    task_run_id: int | None = None,
    github_user: str = "",
    branch_name: str = "",
) -> int | None:
    """Insert a MergeQueueEntry. Returns the entry ID, or None on duplicate."""
    from sqlalchemy import select

    from sova.db.models import MergeQueueEntry
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                existing = await session.execute(
                    select(MergeQueueEntry).where(
                        MergeQueueEntry.pr_number == pr_number,
                        MergeQueueEntry.repo == repo,
                        MergeQueueEntry.status == "queued",
                    )
                )
                if existing.scalars().first() is not None:
                    log.info("merge_queue.entry_exists", pr=pr_number, repo=repo)
                    return None

                entry = MergeQueueEntry(
                    pr_number=pr_number,
                    repo=repo,
                    issue_number=str(issue_number) if issue_number else None,
                    project_dir=str(project_dir),
                    enqueued_at=datetime.now(timezone.utc),
                    task_run_id=task_run_id,
                    github_user=github_user,
                    branch_name=branch_name,
                )
                session.add(entry)
                await session.flush()
                entry_id = entry.id

        log.info("merge_queue.entry_created", entry_id=entry_id, pr=pr_number, repo=repo)
        return entry_id
    except Exception:
        log.warning("merge_queue.create_failed", pr=pr_number, repo=repo, exc_info=True)
        return None


async def _load_queued_entries(project_dir: Path) -> list[dict]:
    """Load all queued entries from the DB."""
    from sqlalchemy import select

    from sova.db.models import MergeQueueEntry
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            result = await session.execute(
                select(MergeQueueEntry).where(
                    MergeQueueEntry.status == "queued",
                    MergeQueueEntry.project_dir == str(project_dir),
                )
            )
            entries = result.scalars().all()
            return [
                {
                    "id": e.id,
                    "pr_number": e.pr_number,
                    "repo": e.repo,
                    "issue_number": e.issue_number,
                    "project_dir": e.project_dir,
                    "enqueued_at": e.enqueued_at,
                    "task_run_id": e.task_run_id,
                    "github_user": e.github_user,
                    "branch_name": e.branch_name,
                }
                for e in entries
            ]
    except Exception:
        log.warning("merge_queue.load_failed", exc_info=True)
        return []


async def _update_entry_status(entry_id: int, status: str, project_dir: Path) -> None:
    """Update a merge queue entry's status."""
    from sqlalchemy import select

    from sova.db.models import MergeQueueEntry
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                result = await session.execute(select(MergeQueueEntry).where(MergeQueueEntry.id == entry_id))
                entry = result.scalars().first()
                if entry is None:
                    return
                entry.status = status
                entry.resolved_at = datetime.now(timezone.utc)
    except Exception:
        log.warning("merge_queue.update_failed", entry_id=entry_id, status=status, exc_info=True)


def create_monitors_for_merge_queue() -> list[MergeQueueMonitor]:
    """Create MergeQueueMonitor instances for all registered projects."""
    from sova.config.loader import load_config
    from sova.config.registry import list_projects

    monitors: list[MergeQueueMonitor] = []
    for path_str in list_projects().values():
        p = Path(path_str)
        if not p.is_dir():
            continue
        try:
            pcfg = load_config(p)
        except Exception:
            log.warning("merge_queue_monitor.config_load_failed", project=str(p), exc_info=True)
            continue
        if pcfg.integration.merge_queue_enabled == "false":
            continue
        if not pcfg.github_repo:
            continue
        monitors.append(
            MergeQueueMonitor(
                project_dir=p,
                repo=pcfg.github_repo,
                github_user=pcfg.github_user,
                integration_config=pcfg.integration,
                notification_config=pcfg.notification,
            )
        )
    return monitors

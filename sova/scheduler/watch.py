"""Async watch loop with priority scan and role dispatch.

Continuously polls the task adapter for actionable issues, orders them by
pipeline readiness, and dispatches the appropriate role for each one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import AdapterError, Task, TaskFilters, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.roles.dispatcher import dispatch
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter
    from sova.scheduler.parallel import ParallelExecutor

log = get_logger(component="scheduler.watch")

# Pipeline stage priority: further along = higher priority (lower number)
_STATE_PRIORITY: dict[TaskState, int] = {
    TaskState.RESEARCHED: 0,
    TaskState.IN_PROGRESS: 1,
    TaskState.TRIAGED: 2,
    TaskState.BACKLOG: 3,
}

_ACTIONABLE_STATES = frozenset(
    {
        TaskState.BACKLOG,
        TaskState.TRIAGED,
        TaskState.RESEARCHED,
        TaskState.IN_PROGRESS,
    }
)


class WatchLoop:
    """Async watch loop that polls for tasks and dispatches roles.

    The loop:
    1. Scans the adapter for actionable tasks
    2. Orders by pipeline stage (most-ready first)
    3. Checks the veto window (optional delay for human override)
    4. Dispatches the appropriate role via the role dispatcher
    """

    def __init__(
        self,
        *,
        config: ProjectConfig,
        adapter: TaskAdapter,
        executor: ParallelExecutor | None = None,
        project_dir: str | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._executor = executor
        self._project_dir = project_dir
        self._running = False
        self._stop_event = asyncio.Event()

        self._start_time = time.monotonic()
        self._scan_count = 0
        self._error_count = 0
        self._last_scan_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        """Signal the watch loop to stop after the current cycle."""
        self._running = False
        self._stop_event.set()

    async def scan(self) -> list[Task]:
        """Scan the adapter for actionable tasks, ordered by pipeline priority."""
        try:
            tasks = await self._adapter.list_tasks(TaskFilters(state="open"))
        except AdapterError:
            log.warning("scan.list_tasks_failed", exc_info=True)
            return []
        actionable = [t for t in tasks if t.state in _ACTIONABLE_STATES]
        actionable.sort(key=lambda t: _STATE_PRIORITY.get(t.state, 99))

        self._scan_count += 1
        self._last_scan_at = datetime.now(UTC)

        return actionable

    async def check_veto(self, task: Task) -> bool:
        """Wait for the veto window, allowing humans to override.

        Returns True if the task should proceed, False if vetoed.
        Currently, the veto window is a simple delay. A future version
        could check for dashboard veto signals.
        """
        veto_seconds = self._config.watch.veto_seconds
        if veto_seconds <= 0:
            return True

        log.info("veto.waiting", task_id=task.id, seconds=veto_seconds)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=veto_seconds)
            # Stop event was set during veto window
            return False
        except asyncio.TimeoutError:
            # Veto window expired without interruption -- proceed
            return True

    async def process_task(self, task: Task) -> bool:
        """Dispatch the appropriate role for a single task.

        Returns True on success, False on failure.
        """
        project_dir = Path(self._project_dir) if self._project_dir else Path.cwd()

        ctx = ExecutionContext(
            project_dir=project_dir,
            config=self._config,
            adapter=self._adapter,
            issue_number=task.id,
            role=self._config.roles.default,
        )

        try:
            role, result = await dispatch(ctx, config=self._config.roles)

            if result.success:
                log.info("task.completed", task_id=task.id, role=role.name, summary=result.summary)
                return True

            log.warning("task.failed", task_id=task.id, role=role.name, error=result.error)
            self._error_count += 1
            return False

        except Exception:
            log.error("task.exception", task_id=task.id, exc_info=True)
            self._error_count += 1
            return False

    async def run(self) -> None:
        """Main watch loop. Runs until stop() is called or interrupted."""
        self._running = True
        self._stop_event.clear()

        log.info(
            "watch.started",
            interval_active=self._config.watch.interval_active,
            interval_idle=self._config.watch.interval_idle,
        )

        try:
            while self._running:
                try:
                    actionable = await self.scan()

                    if actionable:
                        task = actionable[0]
                        log.info("watch.picked", task_id=task.id, title=task.title)

                        if await self.check_veto(task):
                            await self.process_task(task)

                        interval = self._config.watch.interval_active
                    else:
                        log.debug("watch.idle")
                        interval = self._config.watch.interval_idle

                except Exception:
                    log.error("watch.cycle_error", exc_info=True)
                    self._error_count += 1
                    interval = self._config.watch.interval_active

                if not self._running:
                    break

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break  # stop() was called
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue loop

        finally:
            self._running = False
            log.info("watch.stopped")

    def health(self) -> dict:
        """Return health metrics for monitoring."""
        uptime = time.monotonic() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "scans_total": self._scan_count,
            "errors_total": self._error_count,
            "last_scan_at": self._last_scan_at.isoformat() if self._last_scan_at else None,
        }

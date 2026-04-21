"""Concurrent task execution with resource limits.

Uses asyncio.Semaphore to enforce max_parallel_agents from config.
Each task gets its own ExecutionContext and is dispatched independently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import Task
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.roles.dispatcher import dispatch
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter

log = get_logger(component="scheduler.parallel")


@dataclass
class TaskResult:
    """Result of a parallel task execution."""

    task_id: str
    success: bool
    summary: str = ""
    error: str = ""
    role: str = ""


class ParallelExecutor:
    """Executes multiple tasks concurrently with a configurable limit.

    Uses asyncio.Semaphore to cap the number of simultaneously running
    agent dispatches at ``config.max_parallel_agents``.
    """

    def __init__(self, *, config: ProjectConfig, project_dir: Path | None = None) -> None:
        self._config = config
        self._project_dir = project_dir or Path.cwd()
        self._semaphore = asyncio.Semaphore(config.max_parallel_agents)
        self._active: set[asyncio.Task[TaskResult]] = set()

    @property
    def max_concurrent(self) -> int:
        return self._config.max_parallel_agents

    @property
    def active_count(self) -> int:
        return len([t for t in self._active if not t.done()])

    async def execute_tasks(
        self,
        tasks: list[Task],
        *,
        adapter: TaskAdapter,
        force: bool = False,
    ) -> list[TaskResult]:
        """Execute a batch of tasks concurrently, respecting resource limits.

        Returns a list of TaskResult objects, one per input task.
        """
        if not tasks:
            return []

        log.info("parallel.start", count=len(tasks), max_concurrent=self.max_concurrent)

        async def run_one(task: Task) -> TaskResult:
            async with self._semaphore:
                return await self._dispatch_task(task, adapter=adapter, force=force)

        # Create asyncio tasks and track them
        coros = [run_one(t) for t in tasks]
        async_tasks = [asyncio.create_task(c) for c in coros]
        self._active.update(async_tasks)

        try:
            results = await asyncio.gather(*async_tasks, return_exceptions=True)
        finally:
            self._active -= set(async_tasks)

        # Convert exceptions to TaskResults
        task_results: list[TaskResult] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                task_results.append(
                    TaskResult(
                        task_id=tasks[i].id,
                        success=False,
                        error=str(result),
                    )
                )
            else:
                task_results.append(result)

        succeeded = sum(1 for r in task_results if r.success)
        failed = sum(1 for r in task_results if not r.success)
        log.info("parallel.done", succeeded=succeeded, failed=failed)

        return task_results

    async def _dispatch_task(
        self,
        task: Task,
        *,
        adapter: TaskAdapter,
        force: bool = False,
    ) -> TaskResult:
        """Dispatch a single task to the role dispatcher."""
        ctx = ExecutionContext(
            project_dir=self._project_dir,
            config=self._config,
            adapter=adapter,
            issue_number=task.id,
            role=self._config.roles.default,
            force=force,
        )

        try:
            role, result = await dispatch(ctx, config=self._config.roles)

            if result.success:
                log.info("task.completed", task_id=task.id, role=role.name)
                return TaskResult(
                    task_id=task.id,
                    success=True,
                    summary=result.summary,
                    role=role.name,
                )
            else:
                log.warning("task.failed", task_id=task.id, role=role.name, error=result.error)
                return TaskResult(
                    task_id=task.id,
                    success=False,
                    error=result.error or "Unknown failure",
                    role=role.name,
                )

        except Exception as exc:
            log.error("task.exception", task_id=task.id, exc_info=True)
            return TaskResult(
                task_id=task.id,
                success=False,
                error=str(exc),
            )

    async def stop(self) -> None:
        """Cancel all active tasks."""
        for task in list(self._active):
            if not task.done():
                task.cancel()

        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)
            self._active.clear()

        log.info("parallel.stopped")
